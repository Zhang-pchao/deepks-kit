import abc
import time
import torch
import numpy as np
from torch import nn
from pyscf import lib
from pyscf.lib import logger
from pyscf import gto
from pyscf import scf, dft
from deepks.utils import load_basis, get_shell_sec
from deepks.model.model import CorrNet
from deepks.scf.penalty import PenaltyMixin

DEVICE = 'cpu'#torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

# all variables and functions start with "t_" are torch based.
# all variables and functions ends with "0" are original base method results
# convention in einsum:
#   i,j: orbital
#   a,b: atom
#   p,q: projected basis on atom
#   r,s: mol basis in pyscf
# parameter shapes:
#   ovlp_shells: [nao x natom x nsph] list
#   pdm_shells: [natom x nsph x nsph] list
#   eig_shells: [natom x nsph] list


def t_make_pdm(dm, ovlp_shells):
    """return projected density matrix by shell"""
    # (D^I_rl)_mm' = \sum_i < alpha^I_rlm | phi_i >< phi_i | aplha^I_rlm' >
    pdm_shells = [torch.einsum('rap,...rs,saq->...apq', po, dm, po)
                    for po in ovlp_shells]
    return pdm_shells


def t_shell_eig(pdm):
    return torch.symeig(pdm, eigenvectors=True)[0]


def t_make_eig(dm, ovlp_shells):
    """return eigenvalues of projected density matrix"""
    pdm_shells = t_make_pdm(dm, ovlp_shells)
    eig_shells = [t_shell_eig(dm) for dm in pdm_shells]
    ceig = torch.cat(eig_shells, dim=-1)
    return ceig


def t_get_corr(model, dm, ovlp_shells, with_vc=True):
    """return the "correction" energy (and potential) given by a NN model"""
    dm.requires_grad_(True)
    ceig = t_make_eig(dm, ovlp_shells) # natoms x nproj
    _dref = next(model.parameters()) if isinstance(model, nn.Module) else DEVICE
    ec = model(ceig.to(_dref))  # no batch dim here, unsqueeze(0) if needed
    if not with_vc:
        return ec.to(ceig)
    [vc] = torch.autograd.grad(ec, dm, torch.ones_like(ec))
    return ec.to(ceig), vc


def gen_proj_mol(mol, basis) :
    mole_coords = mol.atom_coords(unit="Ang")
    test_mol = gto.Mole()
    test_mol.atom = [["Ne", coord] for coord in mole_coords]
    test_mol.basis = basis
    test_mol.build(0,0,unit="Ang")
    return test_mol


class CorrMixin(abc.ABC):
    """Abstruct mixin class to add "correction" term to the mean-field Hamiltionian"""

    def get_veff0(self, *args, **kwargs):
        return super().get_veff(*args, **kwargs)

    def energy_elec0(self, dm=None, h1e=None, vhf=None):
        if vhf is None: vhf = self.get_veff0(dm=dm)
        return super().energy_elec(dm, h1e, vhf)
    
    def energy_tot0(self, dm=None, h1e=None, vhf=None):
        return self.energy_elec0(dm, h1e, vhf)[0] + self.energy_nuc()

    def nuc_grad_method0(self):
        return super().nuc_grad_method()

    def get_veff(self, mol=None, dm=None, dm_last=0, vhf_last=0, hermi=1):
        """original mean field potential + correction potential"""
        if mol is None: 
            mol = self.mol
        if dm is None: 
            dm = self.make_rdm1()
        tic = (time.clock(), time.time())
        # base method part
        v0_last = getattr(vhf_last, 'v0', 0)
        v0 = self.get_veff0(mol, dm, dm_last, v0_last, hermi)
        tic = logger.timer(self, 'v0', *tic)
        # Correlation (or correction) part
        ec, vc = self.get_corr(dm)
        tic = logger.timer(self, 'vc', *tic)
        # make total effective potential
        vtot = v0 + vc
        vtot = lib.tag_array(vtot, ec=ec, v0=v0)
        return vtot

    def energy_elec(self, dm=None, h1e=None, vhf=None):
        """return electronic energy and the 2-electron part contribution"""
        if dm is None: 
            dm = self.make_rdm1()
        if h1e is None: 
            h1e = self.get_hcore()
        if vhf is None or getattr(vhf, 'ec', None) is None: 
            vhf = self.get_veff(dm=dm)
        etot, e2 = self.energy_elec0(dm, h1e, vhf.v0)
        ec = vhf.ec
        logger.debug(self, f'Emodel = {ec}')
        return (etot+ec).real, e2+ec

    @abc.abstractmethod
    def get_corr(self, dm=None):
        """return "correction" energy and corresponding potential"""
        if dm is None: 
            dm = self.make_rdm1()
        return 0., np.zeros_like(dm)

    @abc.abstractmethod
    def nuc_grad_method(self):
        return self.nuc_grad_method0()


class NetMixin(CorrMixin):
    """Mixin class to add correction term given by a neural network model"""

    def __init__(self, model, proj_basis=None, device=DEVICE):
        # make sure you call this method after the base SCF class init
        # otherwise it would throw an error due to the lack of mol attr
        self.device = device
        if isinstance(model, str):
            model = CorrNet.load(model).double()
        if isinstance(model, torch.nn.Module):
            model = model.to(self.device).eval()
        self.net = model
        # try load basis from model file
        if proj_basis is None:
            proj_basis = getattr(model, "_pbas", None)
        # should be a list here, follow pyscf convention
        self._pbas = load_basis(proj_basis)
        # [1,1,1,...,3,3,3,...,5,5,5,...]
        self._shell_sec = get_shell_sec(self._pbas)
        # total number of projected basis per atom
        self.nproj = sum(self._shell_sec)
        # prepare overlap integrals used in projection
        self.prepare_integrals()

    def prepare_integrals(self):
        # a virtual molecule to be projected on
        self._pmol = gen_proj_mol(self.mol, self._pbas)
        # < mol_ao | alpha^I_rlm >, shape=[nao x natom x nproj]
        t_proj_ovlp = torch.from_numpy(self.proj_ovlp()).double()
        # split the projected coeffs by shell (different r and l)
        self._t_ovlp_shells = torch.split(t_proj_ovlp, self._shell_sec, -1)

    def get_corr(self, dm=None):
        """return "correction" energy and corresponding potential"""
        if dm is None:
            dm = self.make_rdm1()
        if self.net is None:
            return 0., np.zeros_like(dm)
        dm = np.asanyarray(dm)
        if dm.ndim >= 3 and isinstance(self, scf.uhf.UHF):
            dm = dm.sum(0)
        t_dm = torch.from_numpy(dm).double()
        t_ec, t_vc = t_get_corr(self.net, t_dm, self._t_ovlp_shells, with_vc=True)
        return (t_ec.item() if t_ec.nelement()==1 else t_ec.detach().cpu().numpy(), 
                t_vc.detach().cpu().numpy())

    def nuc_grad_method(self):
        from deepks.scf.grad import build_grad
        return build_grad(self)

    def reset(self, mol=None):
        super().reset(mol)
        self.prepare_integrals()
        return self

    def make_pdm(self, dm=None, flatten=False):
        """return projected density matrix by shell"""
        if dm is None:
            dm = self.make_rdm1()
        t_dm = torch.from_numpy(dm).double()
        t_pdm_shells = t_make_pdm(t_dm, self._t_ovlp_shells)
        if not flatten:
            return [s.detach().cpu().numpy() for s in t_pdm_shells]
        else:
            return torch.cat([s.flatten(-2) for s in t_pdm_shells], 
                             dim=-1).detach().cpu().numpy()

    def make_eig(self, dm=None):
        """return eigenvalues of projected density matrix"""
        if dm is None:
            dm = self.make_rdm1()
        dm = np.asanyarray(dm)
        if dm.ndim >= 3 and isinstance(self, scf.uhf.UHF):
            dm = dm.sum(0)
        t_dm = torch.from_numpy(dm).double()
        t_eig = t_make_eig(t_dm, self._t_ovlp_shells)
        return t_eig.detach().cpu().numpy()

    def proj_intor(self, intor):
        """1-electron integrals between origin and projected basis"""
        proj = gto.intor_cross(intor, self.mol, self._pmol) 
        return proj
        
    def proj_ovlp(self):
        """overlap between origin and projected basis, reshaped"""
        nao = self.mol.nao
        natm = self.mol.natm
        pnao = self._pmol.nao
        proj = self.proj_intor("int1e_ovlp")
        # return shape [nao x natom x nproj]
        return proj.reshape(nao, natm, pnao // natm)

    def gen_coul_loss(self, fock=None, ovlp=None, mo_occ=None):
        nao = self.mol.nao
        fock = (fock if fock is not None else self.get_fock()).reshape(-1, nao, nao)
        s1e = ovlp if ovlp is not None else self.get_ovlp()
        mo_occ = (mo_occ if mo_occ is not None else self.mo_occ).reshape(-1, nao)
        def _coul_loss_grad(v, target_dm):
            # return coulomb loss and its grad with respect to fock matrix
            # only support single dm, do not use directly for UHF
            a_loss = 0.
            a_grad = 0.
            target_dm = target_dm.reshape(fock.shape)
            for tdm, f1e, nocc in zip(target_dm, fock, mo_occ):
                iocc = nocc>0
                moe, moc = self._eigh(f1e+v, s1e)
                eo, ev = moe[iocc], moe[~iocc]
                co, cv = moc[:, iocc], moc[:, ~iocc]
                dm = (co * nocc[iocc]) @ co.T
                # calc loss
                ddm = dm - tdm
                dvj = self.get_j(dm=ddm)
                loss = 0.5 * np.einsum("ij,ji", ddm, dvj)
                a_loss += loss
                # calc grad with respect to fock matrix
                ie_mn = 1. / (-ev.reshape(-1, 1) + eo)
                temp_mn = cv.T @ dvj @ co * nocc[iocc] * ie_mn
                dldv = cv @ temp_mn @ co.T
                dldv = dldv + dldv.T
                a_grad += dldv
            return a_loss, a_grad
        return _coul_loss_grad
    
    def make_grad_coul_veig(self, target_dm):
        clfn = self.gen_coul_loss()
        dm = self.make_rdm1()
        if dm.ndim == 3 and isinstance(self, scf.uhf.UHF):
            dm = dm.sum(0)
        t_dm = torch.from_numpy(dm).requires_grad_()
        t_eig = t_make_eig(t_dm, self._t_ovlp_shells).requires_grad_()
        loss, dldv = clfn(np.zeros_like(dm), target_dm)
        t_veig = torch.zeros_like(t_eig).requires_grad_()
        [t_vc] = torch.autograd.grad(t_eig, t_dm, t_veig, create_graph=True)
        [t_ghead] = torch.autograd.grad(t_vc, t_veig, torch.from_numpy(dldv))
        return t_ghead.detach().cpu().numpy()
    
    def calc_optim_veig(self, target_dm, 
                        target_dec=None, gvx=None, 
                        nstep=1, force_factor=1., **optim_args):
        clfn = self.gen_coul_loss(fock=self.get_fock(vhf=self.get_veff0()))
        dm = self.make_rdm1()
        if dm.ndim == 3 and isinstance(self, scf.uhf.UHF):
            dm = dm.sum(0)
        t_dm = torch.from_numpy(dm).requires_grad_()
        t_eig = t_make_eig(t_dm, self._t_ovlp_shells).requires_grad_()
        t_ec = self.net(t_eig.to(self.device))
        t_veig = torch.autograd.grad(t_ec, t_eig)[0].requires_grad_()
        t_lde = torch.from_numpy(target_dec) if target_dec is not None else None
        t_gvx = torch.from_numpy(gvx) if gvx is not None else None
        # build closure
        def closure():
            [t_vc] = torch.autograd.grad(
                t_eig, t_dm, t_veig, retain_graph=True, create_graph=True)
            loss, dldv = clfn(t_vc.detach().numpy(), target_dm)
            grad = torch.autograd.grad(
                t_vc, t_veig, torch.from_numpy(dldv), only_inputs=True)[0]
            # build closure for force loss
            if t_lde is not None and t_gvx is not None:
                t_pde = torch.tensordot(t_gvx, t_veig)
                lossde = force_factor * torch.sum((t_pde - t_lde)**2)
                grad = grad + torch.autograd.grad(lossde, t_veig, only_inputs=True)[0]
                loss = loss + lossde
            t_veig.grad = grad
            return loss
        # do the optimization
        optim = torch.optim.LBFGS([t_veig], **optim_args)
        tic = (time.clock(), time.time())
        for _ in range(nstep):
            optim.step(closure)
            tic = logger.timer(self, 'LBFGS step', *tic)
        logger.note(self, f"optimized loss for veig = {closure()}")        
        return t_veig.detach().numpy()


class DSCF(NetMixin, PenaltyMixin, dft.rks.RKS):
    """Restricted SCF solver for given NN energy model"""
    
    def __init__(self, mol, model, xc="HF", proj_basis=None, penalties=None, device=DEVICE):
        # base method must be initialized first
        dft.rks.RKS.__init__(self, mol, xc=xc)
        # correction mixin initialization
        NetMixin.__init__(self, model, proj_basis=proj_basis, device=device)
        # penalty term initialization
        PenaltyMixin.__init__(self, penalties=penalties)
        # update keys to avoid pyscf warning
        self._keys.update(self.__dict__.keys())

DeepSCF = RDSCF = DSCF


class UDSCF(NetMixin, PenaltyMixin, dft.uks.UKS):
    """Unrestricted SCF solver for given NN energy model"""
    
    def __init__(self, mol, model, xc="HF", proj_basis=None, penalties=None, device=DEVICE):
        # base method must be initialized first
        dft.uks.UKS.__init__(self, mol, xc=xc)
        # correction mixin initialization
        NetMixin.__init__(self, model, proj_basis=proj_basis, device=device)
        # penalty term initialization
        PenaltyMixin.__init__(self, penalties=penalties)
        # update keys to avoid pyscf warning
        self._keys.update(self.__dict__.keys())

# if __name__ == '__main__':
#     mol = gto.Mole()
#     mol.verbose = 5
#     mol.output = None
#     mol.atom = [['He', (0, 0, 0)], ]
#     mol.basis = 'ccpvdz'
#     mol.build(0, 0)

#     def test_model(eigs):
#         assert eigs.shape[-1] == _zeta.size * 9
#         return 1e-3 * torch.sum(eigs, axis=(1,2))
    
#     # SCF Procedure
#     dscf = DSCF(mol, test_model)
#     energy = dscf.kernel()
#     print(energy)
