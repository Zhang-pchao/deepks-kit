import time
import torch
import numpy as np
from pyscf import gto, lib
from pyscf.lib import logger
from pyscf.grad import rks as grad_base

# see ./_old_grad.py for a more clear (but maybe slower) implementation
# all variables and functions start with "t_" are torch related.
# convention in einsum:
#   i,j: orbital
#   a,b: atom
#   p,q: projected basis on atom
#   r,s: mol basis in pyscf
#   x  : space component of gradient
#   v  : eigen values of projected dm
# parameter shapes:
#   ovlp_shells: [nao x natom x nsph] list
#   pdm_shells: [natom x nsph x nsph] list
#   eig_shells: [natom x nsph] list
#   ipov_shells: [3 x nao x natom x nsph] list
#   gdmx_shells: [natm (deriv atom) x 3 x natm (proj atom) x nsph x nsph] list
#   gedm_shells: [natom x nsph x nsph] list


def t_make_grad_e_pdm(model, dm, ovlp_shells):
    """return gradient of energy w.r.t projected density matrix"""
    # calculate \partial E / \partial (D^I_rl)_mm' by shells
    pdm_shells = [torch.einsum('rap,rs,saq->apq', po, dm, po).requires_grad_(True)
                        for po in ovlp_shells]
    eig_shells = [torch.symeig(dm, eigenvectors=True)[0]
                        for dm in pdm_shells]
    ceig = torch.cat(eig_shells, dim=-1).unsqueeze(0) # 1 x natoms x nproj
    _dref = next(model.parameters())
    ec = model(ceig.to(_dref))  # no batch dim here, unsqueeze(0) if needed
    gedm_shells = torch.autograd.grad(ec, pdm_shells)
    return gedm_shells


def t_make_grad_pdm_x(mol, dm, ovlp_shells, ipov_shells):
    """return jacobian of projected density matrix w.r.t atomic coordinates"""
    natm = mol.natm
    shell_sec = [ov.shape[-1] for ov in ovlp_shells]
    # [natm (deriv atom) x 3 (xyz) x natm (proj atom) x nsph (1|3|5) x nsph] list
    gdmx_shells = [torch.zeros([natm, 3, natm, ss, ss], dtype=float) 
                        for ss in shell_sec]
    for gdmx, govx, ovlp in zip(gdmx_shells, ipov_shells, ovlp_shells):
        # contribution of projection for all I
        gproj = torch.einsum('xrap,rs,saq->xapq', govx, dm, ovlp)
        for ia in range(natm):
            bg, ed = mol.aoslice_by_atom()[ia, 2:]
            # contribution of < \nabla mol_ao |
            gdmx[ia] -= torch.einsum('xrap,rs,saq->xapq', govx[:,bg:ed], dm[bg:ed], ovlp)
            # contribution of | \nabla alpha^I_rlm >
            gdmx[ia,:,ia] += gproj[:, ia]
        # symmetrize p and q
        gdmx += gdmx.clone().transpose(-1,-2)
    return gdmx_shells


def t_make_grad_eig_x(mol, dm, ovlp_shells, ipov_shells):
    """return jacobian of decriptor eigenvalues w.r.t atomic coordinates"""
    # v stands for eigen values
    pdm_shells = [torch.einsum('rap,rs,saq->apq', po, dm, po).requires_grad_(True)
                        for po in ovlp_shells]
    calc_eig = lambda dm: torch.symeig(dm, True)[0]
    gvdm_shells = [t_batch_jacobian(calc_eig, dm, dm.shape[-1]) 
                        for dm in pdm_shells]
    gdmx_shells = t_make_grad_pdm_x(mol, dm, ovlp_shells, ipov_shells)
    gvx_shells = [torch.einsum("bxapq,avpq->bxav", gdmx, gvdm) 
                        for gdmx, gvdm in zip(gdmx_shells, gvdm_shells)]
    return torch.cat(gvx_shells, dim=-1)


def t_grad_corr(mol, model, dm, ovlp_shells, ipov_shells, atmlst=None):
    if atmlst is None:
        atmlst = list(range(mol.natm))
    dec = torch.zeros([len(atmlst), 3], dtype=float)
    # \partial E / \partial (D^I_rl)_mm' by shells
    gedm_shells = t_make_grad_e_pdm(model, dm, ovlp_shells)
    for gedm, govx, ovlp in zip(gedm_shells, ipov_shells, ovlp_shells):
        # contribution of projection orbitals for all atom
        ginner = torch.einsum('xrap,rs,saq->xapq', govx, dm, ovlp) * 2
        # contribution of atomic orbitals for all atom
        gouter = -torch.einsum('xrap,apq,saq->xrs', govx, gedm, ovlp) * 2
        for k, ia in enumerate(atmlst):
            bg, ed = mol.aoslice_by_atom()[ia, 2:]
            # contribution of | \nabla alpha^I_rlm > and < \nabla alpha^I_rlm |
            dec[k] += torch.einsum('xpq,pq->x', ginner[:,ia], gedm[ia])
            # contribution of < \nabla mol_ao | and | \nabla mol_ao >
            dec[k] += torch.einsum('xrs,rs->x', gouter[:,bg:ed], dm[bg:ed])
    return dec


def t_batch_jacobian(f, x, noutputs):
    nindim = len(x.shape)-1
    x = x.unsqueeze(1) # b, 1 ,*in_dim
    n = x.shape[0]
    x = x.repeat(1, noutputs, *[1]*nindim) # b, out_dim, *in_dim
    x.requires_grad_(True)
    y = f(x)
    input_val = torch.eye(noutputs).reshape(1,noutputs, noutputs).repeat(n, 1, 1)
    return torch.autograd.grad(y, x, input_val)[0]


class Gradients(grad_base.Gradients):
    """Analytical nuclear gradient for the DeePKS model"""
    
    def __init__(self, mf):
        super().__init__(mf)
        # prepare integrals for projection and derivative
        self.prepare_integrals()
        # add a field to memorize the pulay term in ec
        self.dec = None
        self._keys.update(self.__dict__.keys())

    def prepare_integrals(self):
        mf = self.base
        # < mol_ao | alpha^I_rlm > by shells
        self._t_ovlp_shells = mf._t_ovlp_shells
        # < \nabla mol_ao | alpha^I_rlm >
        t_proj_ipovlp = torch.from_numpy(mf.proj_intor("int1e_ipovlp")).double()
        # < \nabla mol_ao | alpha^I_rlm > by shells
        self._t_ipov_shells = torch.split(
            t_proj_ipovlp.reshape(3, self.mol.nao, self.mol.natm, -1), 
            self.base._shell_sec, -1)
    
    def grad_elec(self, mo_energy=None, mo_coeff=None, mo_occ=None, atmlst=None):
        de = super().grad_elec(mo_energy, mo_coeff, mo_occ, atmlst)
        cput0 = (time.clock(), time.time())
        dec = self.grad_corr(self.base.make_rdm1(mo_coeff, mo_occ), atmlst)
        logger.timer(self, 'gradients of NN pulay part', *cput0)
        # memeorize the result to save time in get_base
        self.dec = self.symmetrize(dec, atmlst) if self.mol.symmetry else dec
        return de + dec

    def get_base(self):
        """return the grad given by raw base method Hamiltonian under current dm"""
        assert self.de is not None and self.dec is not None
        return self.de - self.dec
        
    def grad_corr(self, dm=None, atmlst=None):
        """additional contribution of NN "correction" term resulted from projection"""
        if atmlst is None:
            atmlst = range(self.mol.natm)
        if self.base.net is None:
            return np.zeros([len(atmlst), 3])
        if dm is None:
            dm = self.base.make_rdm1()
        t_dm = torch.from_numpy(dm).double()
        t_dec = t_grad_corr(self.mol, self.base.net, t_dm, 
                            self._t_ovlp_shells, self._t_ipov_shells, atmlst)
        return t_dec.detach().cpu().numpy()

    def make_grad_pdm_x(self, dm=None, flatten=False):
        """return jacobian of projected density matrix w.r.t atomic coordinates"""
        if dm is None:
            dm = self.base.make_rdm1()
        t_dm = torch.from_numpy(dm).double()
        t_gdmx_shells = t_make_grad_pdm_x(self.mol, t_dm, 
                            self._t_ovlp_shells, self._t_ipov_shells)
        if not flatten:
            return [s.detach().cpu().numpy() for s in t_gdmx_shells]
        else:
            return torch.cat([s.flatten(-2) for s in t_gdmx_shells], 
                             dim=-1).detach().cpu().numpy()

    def make_grad_eig_x(self, dm=None):
        """return jacobian of decriptor eigenvalues w.r.t atomic coordinates"""
        if dm is None:
            dm = self.base.make_rdm1()
        t_dm = torch.from_numpy(dm).double()
        t_gvx = t_make_grad_eig_x(self.mol, t_dm, 
                    self._t_ovlp_shells, self._t_ipov_shells)
        return t_gvx.detach().cpu().numpy()

    def as_scanner(self):
        scanner = super().as_scanner()
        # make a new version of call method
        class NewScanner(type(scanner)):
            def __call__(self, mol_or_geom, **kwargs):
                if isinstance(mol_or_geom, gto.Mole):
                    mol = mol_or_geom
                else:
                    mol = self.mol.set_geom_(mol_or_geom, inplace=False)

                mf_scanner = self.base
                e_tot = mf_scanner(mol)
                self.mol = mol

                if getattr(self, 'grids', None):
                    self.grids.reset(mol)
                # adding the following line to refresh integrals
                self.prepare_integrals()
                de = self.kernel(**kwargs)
                return e_tot, de

        # hecking the old scanner's method, bind the new one
        scanner.__class__ = NewScanner
        return scanner


# legacy method, kept for reference
def make_mask(mol1, mol2, atom_id):
    mask = np.zeros((mol1.nao, mol2.nao))
    bg1, ed1 = mol1.aoslice_by_atom()[atom_id, 2:]
    bg2, ed2 = mol2.aoslice_by_atom()[atom_id, 2:]
    mask[bg1:ed1, :] -= 1
    mask[:, bg2:ed2] += 1
    return mask


# only for testing purpose, not used in code
def finite_difference(f, x, delta=1e-6):
    in_shape = x.shape
    y0 = f(x)
    out_shape = y0.shape
    res = np.empty(in_shape + out_shape)
    for idx in np.ndindex(*in_shape):
        diff = np.zeros(in_shape)
        diff[idx] += delta
        y1 = f(x+diff)
        res[idx] = (y1-y0) / delta
    return res


Grad = Gradients

from deepqc.scf.scf import DSCF
# Inject to SCF class
DSCF.Gradients = lib.class_as_method(Gradients)
