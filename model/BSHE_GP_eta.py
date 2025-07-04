import torch
from tqdm import tqdm
import time
import numpy as np
import multiprocessing as mp
from model.helper import ker_approx


def run_single_chain(chain_id, Y, kwargs):
    model = BSHE_voxel_GP(Y, **kwargs)
    model.fit(chain_id, verbose=True)
    return model.get_samples()

def run_multiple_chains(Y, n_chains=4, seeds = 42, parallel = False, **kwargs):
    torch.manual_seed(seeds)

    if parallel:
        ctx = mp.get_context("spawn")  
        with ctx.Pool(processes=n_chains) as pool:
            results = pool.starmap(run_single_chain, [(i, Y, kwargs) for i in range(n_chains)])
    else:
        results=[]
        for i in range(n_chains):
            results.append(run_single_chain(i, Y, kwargs))

    # stack mcmcm samples in each chain
    results_chain = {}
    keys = results[0].keys()
    for key in keys:
        temp = [r[key] for r in results]  
        results_chain[key] = torch.stack(temp, dim=0) # dim: nchain x nsample x ...
    return results_chain

def get_basis(S, L, kernel, dtype=torch.float32):
    V = S.shape[0]
    induce = torch.linspace(0, V, steps=L+2)[1:-1].long()
    S_c = S[induce,:]

    B, eig_val_sqrt = ker_approx(kernel, S, S_c, dtype) #ls, var, nu
    return B, eig_val_sqrt

class BSHE_voxel_GP_eta():
    def __init__(self, Y, grids, kernel, L = 100, L_eta=50, dtype=torch.float32,
                #='Matern', ls = 1.0, nu=1.5,
                burnin=100, thin=1, mcmc_sample=10,
                B = None, eig_val_sqrt=None,
                B_eta = None, eig_val_sqrt_eta=None,
                init_alpha = None, 
                init_theta_eta = None, 
                init_theta_beta = None, 
                init_sig2_e = None, 
                init_sig2_eta = None,
                sig2_alpha = 100,
                A=100,
                ):
  
        self.y = Y.to(dtype) # V by n
        self.N, self.V = Y.shape # num of subVect
        self.dtype=dtype

        ### mcmc settings
        self.mcmc_burnin = burnin
        self.mcmc_thinning = thin
        self.mcmc_sample = mcmc_sample 
        self.total_iter = self.mcmc_burnin + self.mcmc_sample * self.mcmc_thinning

        assert L_eta < L

        if B is None:
            B, eig_val_sqrt = get_basis(grids, L,  kernel, dtype=dtype)
        if B_eta is None:
            B_eta, eig_val_sqrt_eta = get_basis(grids, L_eta,  kernel, dtype=dtype)

        self.L = L
        self.L_eta = L_eta
        self.B_lamb = B * (eig_val_sqrt.unsqueeze(0) )
        self.B_lamb_inv = B * ( (1.0 / eig_val_sqrt).unsqueeze(0))  # V by L

        self.B_lamb_eta_t = (B_eta * (eig_val_sqrt_eta.unsqueeze(0) )).t()
 
        self.y_tilde = self.y @ self.B_lamb_inv
        self.B_lamb_inv_sumv = self.B_lamb_inv.sum(0)
        self.B_lamb_inv_sumv_ssq = (self.B_lamb_inv_sumv ** 2).sum()

        self.B_prime = self.B_lamb_eta_t @ self.B_lamb_inv # L' by L
        self.B_prime_sq = self.B_prime @ self.B_prime.t()

        #initialization
        self.alpha = torch.randn(1, dtype=self.dtype) if init_alpha is None else init_alpha.clone()
        # self.eta = torch.randn(self.N, dtype=self.dtype) if init_eta is None else init_eta.clone()
        # self.eta -= self.eta.mean()

        self.theta_eta = torch.randn(self.N, self.L_eta, dtype=self.dtype) if init_theta_eta is None else init_theta_eta.clone()
        self.eta = self.theta_eta @ self.B_lamb_eta_t
        self.eta -= self.eta.mean(1, keepdim=True)
        
        self.theta_beta = torch.randn(self.L, dtype=self.dtype) if init_theta_beta is None else init_theta_beta.clone()
        self.beta = self.B_lamb @ self.theta_beta
        self.beta -= self.beta.mean()

        self.sig2_eta = torch.rand(1, dtype=self.dtype) if init_sig2_eta is None else init_sig2_eta.clone()
        self.sig2_e = torch.rand(1, dtype=self.dtype) if init_sig2_e is None else init_sig2_e.clone()

        self.sig2_alpha = sig2_alpha
        self.A = A

        self.a_eta, self.a_e = torch.ones(2)
        self.update_res()
        self.loglik_y = torch.zeros(self.total_iter)
        self.make_mcmc_samples()


    def fit(self, chain_id=0, verbose=False, mute=False):
        start_time = time.time()
        for i in tqdm(range(self.total_iter), disable=mute): 
            self.update_alpha()
            self.update_theta_eta()
            self.update_theta_beta()

            #self.update_sig2_eta()
            self.update_sig2_e()

            self.loglik_y[i] = self.update_loglik_y()
            if i >= self.mcmc_burnin:
                if (i - self.mcmc_burnin) % self.mcmc_thinning == 0:
                    mcmc_iter = int((i - self.mcmc_burnin) / self.mcmc_thinning)
                    self.save_mcmc_samples(mcmc_iter)
        self.runtime = time.time() - start_time
        if verbose:
            print(f"Chain {chain_id + 1} finished in {self.runtime:.2f} seconds")
        
   
    def update_res(self):
        self.res = self.y_tilde - self.alpha * self.B_lamb_inv_sumv - self.theta_eta @ self.B_prime - self.theta_beta.unsqueeze(0)

    def update_alpha(self):
        self.res += self.alpha * self.B_lamb_inv_sumv
        sig2 = 1 / ( self.N * self.B_lamb_inv_sumv_ssq / self.sig2_e + 1 / self.sig2_alpha)
        mu = sig2 * (self.B_lamb_inv_sumv / self.sig2_e  * self.res).sum() 
        self.alpha = torch.randn(1) * sig2.sqrt() + mu
        self.res -= self.alpha * self.B_lamb_inv_sumv

    def update_theta_eta(self):
        #print(self.eta)
        self.res += self.theta_eta @ self.B_prime
        precision = self.B_prime_sq / self.sig2_e + torch.eye(self.L_eta) / self.sig2_e
       
        R = torch.linalg.cholesky(precision, upper=True)
        temp = (self.res @ self.B_prime.t()) / self.sig2_e 
        Z = torch.randn(self.N, self.L_eta)

        for i in range(self.N):
            b = torch.linalg.solve(R.t(), temp[i])
            self.theta_eta[i] = torch.linalg.solve(R, Z[i]+b)

        self.theta_eta -= self.theta_eta.mean(1,keepdim=True)
        self.eta = self.theta_eta @ self.B_lamb_eta_t
        self.eta -= self.eta.mean(1, keepdim=True)
        self.res -= self.theta_eta @ self.B_prime
    
    def update_theta_beta(self):
        #print(self.eta)
        self.res += self.theta_beta.unsqueeze(0)
        sig2 = 1 / ( (self.N + 1) / self.sig2_e )
        mu = sig2 * ( self.res / self.sig2_e ).sum(0) 
        self.theta_beta = torch.randn(self.L) * sig2.sqrt() + mu
        self.theta_beta -= self.theta_beta.mean()
        self.beta = self.B_lamb @ self.theta_beta
        self.beta -= self.beta.mean()
        self.res -= self.theta_beta.unsqueeze(0)

    def update_sig2_e(self):
        a_new = (1 + self.L + self.L * self.N + self.L_eta * self.N)/ 2 
        b_new = ( self.theta_beta ** 2).sum() / 2 + ( self.res ** 2).sum() / 2 + ( self.theta_eta ** 2).sum() / 2 + 1 / self.a_e
        m = torch.distributions.Gamma(a_new, b_new)
        self.sig2_e = 1 / m.sample()

        m = torch.distributions.Gamma(1, 1/self.A + 1 / self.sig2_e)
        self.a_e = 1 / m.sample()

    # def update_sig2_eta(self):
    #     a_new = (1 + self.N )/ 2 
    #     b_new = ( self.eta ** 2).sum() / 2 + 1 / self.a_eta
    #     m = torch.distributions.Gamma(a_new, b_new)
    #     self.sig2_eta = 1 / m.sample()

    #     m = torch.distributions.Gamma(1, 1/self.A + 1 / self.sig2_eta)
    #     self.a_eta = 1 / m.sample()

    def update_loglik_y(self):
        logll = (- self.N / 2 * torch.log(2. * torch.pi * self.sig2_e)).sum() - 0.5 * (self.res ** 2 / self.sig2_e ).sum() 
        return logll

    def make_mcmc_samples(self):
        self.mcmc_alpha = torch.zeros(self.mcmc_sample, dtype=self.dtype)
        #self.mcmc_eta = torch.zeros(self.mcmc_sample, self.N, dtype=self.dtype)
        self.mcmc_theta_beta = torch.zeros(self.mcmc_sample, self.L, dtype=self.dtype)
        self.mcmc_theta_eta = torch.zeros(self.mcmc_sample,  self.N, self.L_eta, dtype=self.dtype)

        self.mcmc_sig2_eta = torch.zeros(self.mcmc_sample, dtype=self.dtype)
        self.mcmc_sig2_e = torch.zeros(self.mcmc_sample, dtype=self.dtype)


    def save_mcmc_samples(self, mcmc_iter):
        self.mcmc_alpha[mcmc_iter] = self.alpha
        self.mcmc_theta_eta[mcmc_iter, :,:] = self.theta_eta
        
        self.mcmc_theta_beta[mcmc_iter, :] = self.theta_beta

        self.mcmc_sig2_eta[mcmc_iter] = self.sig2_eta
        self.mcmc_sig2_e[mcmc_iter] = self.sig2_e

    def get_samples(self):
        return {
            "alpha": self.mcmc_alpha,
            "theta_eta": self.mcmc_theta_eta,
            "theta_beta": self.mcmc_theta_beta,
            "sig2_eta": self.mcmc_sig2_eta,
            "sig2_e": self.mcmc_sig2_e,
            "loglik": self.loglik_y,
            'runtime':torch.tensor(self.runtime),
        }
    
    # def get_basis(self):
    #     return self.B_lamb


def PPC(data, samples, basis, basis_eta, n_samples=100, dtype=torch.float32):
    """
    Posterior Predictive Check (PPC)

    Args:
        n_sample (int): number of posterior samples
        dtype (torch.dtype): output tensor dtype

    Returns:
        pred_y: tensor of shape (n_sample, N, V)
    """
    n_chains, n_mcmc = samples['alpha'].shape
    N, V = data.shape
    L = basis.shape[1]
    if n_samples > n_mcmc:
        print("number of draws larger than mcmc samples")
    idx = torch.linspace(0, n_mcmc - 1, n_samples, dtype=torch.int32)
    pred_y = torch.zeros(n_chains, n_samples, N, V, dtype=dtype)
    basis_eta_t = basis_eta.t()
    

    for s in range(n_chains):
        for i, ind in enumerate(idx):
            alpha = samples['alpha'][s, ind]
            theta_eta = samples['theta_eta'][s, ind]
            theta_beta = samples['theta_beta'][s, ind]
            sig2_e = samples['sig2_e'][s, ind]

            mean = alpha + theta_eta @ basis_eta_t + (basis @ theta_beta).unsqueeze(0)
            noise = basis @ (torch.randn(L, N) * sig2_e.sqrt())
            pred_y[s, i] = mean + noise.t()
    return pred_y


def get_ll(data, samples, dtype=torch.float32):
    """
    get log-likelihood for each observation (voxel-wise)

    Args:
        data (tensor): input data
        samples (dic): dictionary contains mcmc samples for model 1

    Returns:
        pred_y: tensor of shape (n_sample, N, V)
    """
    n_chains, n_mcmc = samples['alpha'].shape
    N, V = data.shape
    basis = samples['basis'][0]

    ll_mat = torch.zeros(n_chains, n_mcmc, V,  dtype=dtype)
    for s in range(n_chains):
        for ind in range(n_mcmc):
            alpha = samples['alpha'][s, ind].to(dtype=dtype)
            eta = samples['eta'][s, ind].to(dtype=dtype)
            theta_beta = samples['theta_beta'][s, ind].to(dtype=dtype)
            sig2_eps = samples['sig2_eps'][s, ind].to(dtype=dtype)

            mu = alpha + eta.unsqueeze(-1) + (basis @ theta_beta).unsqueeze(0)
            res = data - mu
            ll = -0.5 * torch.log(2 * torch.pi * sig2_eps) - 0.5 * (res**2) / sig2_eps
            #ll_mat[s,ind] = ll.sum(dim=1) # individual level
            ll_mat[s,ind] = ll.sum(dim=0)
    return ll_mat
    
