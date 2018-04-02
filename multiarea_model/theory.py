"""
theory
============

Theory package to predict the stable fixed points of the multi-area
model of macaque visual cortex (Schmidt et al. 2018), perform further
analysis on them and to apply the stabilization procedure (Schuecker,
Schmidt et al., 2017) to the network connectivity.


Classes
--------
Theory : provides functionality to predict the stable fixed point of
the model, perform further analysis and execute the stabilization
provedure.

"""

import json
import pprint
import multiprocessing
import nest
import numpy as np

from copy import copy
from .default_params import nested_update, theory_params
from .default_params import check_custom_params
from dicthash import dicthash
from functools import partial
from .multiarea_helpers import create_mask, create_vector_mask, dict_to_vector
from .theory_helpers import d_nu_d_mu_fb_numeric, d_nu_d_sigma_fb_numeric
from .theory_helpers import nu0_fb


class Theory():
    def __init__(self, network, theory_spec):
        self.params = copy(theory_params)
        check_custom_params(theory_spec, self.params)
        self.custom_params = theory_spec
        nested_update(self.params, self.custom_params)

        self.network = network
        E_L = self.params['neuron_params']['single_neuron_dict']['E_L']
        self.NP = {'theta': self.params['neuron_params']['single_neuron_dict']['V_th'] - E_L,
                   'V_reset': self.params['neuron_params']['single_neuron_dict']['V_reset'] - E_L,
                   'tau_m': self.params['neuron_params']['single_neuron_dict']['tau_m'],
                   # assumes that tau_syn_ex = tau_syn_in in LIF neuron
                   'tau_syn': self.params['neuron_params']['single_neuron_dict']['tau_syn_ex'],
                   't_ref': self.params['neuron_params']['single_neuron_dict']['t_ref'],
                   'tau': 1.}

        self.label = dicthash.generate_hash_from_dict({'params': self.params,
                                                       'network_label': self.network.label})

    def __eq__(self, other):
        return self.label == other.label

    def __str__(self):
        s = "Analytical theory {} of network {}".format(self.label, self.network.label)
        s += "with parameters:"
        s += pprint.pformat(self.params, width=1)
        return s

    def __hash__(self):
        return hash(self.label)

    def integrate_siegert_nest(self, full_output=True):
        """
        Integrate siegert formula to obtain stationary rates. See Eq. (3)
        and following in Schuecker, Schmidt et al. (2017).
        """
        dt = self.params['dt']
        T = self.params['T']
        rate_ext = self.params['input_params']['rate_ext']
        K = copy(self.network.K_matrix)
        J = copy(self.network.J_matrix)
        tau = self.NP['tau_m'] * 1e-3
        dim = np.shape(K)[0]
        nest.ResetKernel()
        nest.set_verbosity('M_ERROR')
        nest.SetKernelStatus({'resolution': dt,
                              'use_wfr': False,
                              'print_time': False,
                              'overwrite_files': True})
        # create neurons for external drive
        drive = nest.Create(
            'siegert_neuron', 1, params={'rate': rate_ext, 'mean': rate_ext})
        # create neurons representing populations
        neurons = nest.Create(
            'siegert_neuron', dim, params=self.NP)
        # external drive
        syn_dict = {'drift_factor': tau * np.array([K[:, -1] * J[:, -1]]).transpose(),
                    'diffusion_factor': tau * np.array([K[:, -1] * J[:, -1]**2]).transpose(),
                    'model': 'diffusion_connection',
                    'receptor_type': 0}
        nest.Connect(drive, neurons, 'all_to_all', syn_dict)

        # external DC drive (expressed in mV)
        DC_drive = nest.Create(
            'siegert_neuron', 1, params={'rate': 1., 'mean': 1.})

        C_m = self.network.params['neuron_params']['single_neuron_dict']['C_m']
        syn_dict = {'drift_factor': 1e3 * tau / C_m * np.array(
            self.network.add_DC_drive).reshape(dim, 1),
                    'diffusion_factor': 0.,
                    'model': 'diffusion_connection',
                    'receptor_type': 0}
        nest.Connect(DC_drive, neurons, 'all_to_all', syn_dict)
        # handle switches for cortico-cortical connectivity
        if (self.network.params['connection_params']['replace_cc'] in
                ['hom_poisson_stat', 'het_poisson_stat']):
            mu_CC, sigma2_CC = self.replace_cc_input()
            mask = create_mask(self.network.structure, cortico_cortical=True, external=False)
            K[mask] = 0.
            # Additional external drive
            # The actual rate is included in the connection
            add_drive = nest.Create('siegert_neuron', 1, params={'rate': 1., 'mean': 1.})
            syn_dict = {'drift_factor': np.array([mu_CC]).transpose(),
                        'diffusion_factor': np.array([sigma2_CC]).transpose(),
                        'model': 'diffusion_connection',
                        'receptor_type': 0}
            nest.Connect(add_drive, neurons, 'all_to_all', syn_dict)
        elif self.network.params['connection_params']['replace_cc'] == 'het_current_nonstat':
            raise NotImplementedError('Replacing the cortico-cortical input by'
                                      ' non-stationary current input is not supported'
                                      ' in the Theory class.')
        # network connections
        syn_dict = {'drift_factor': tau * K[:, :-1] * J[:, :-1],
                    'diffusion_factor': tau * K[:, :-1] * J[:, :-1]**2,
                    'model': 'diffusion_connection',
                    'receptor_type': 0}
        nest.Connect(neurons, neurons, 'all_to_all', syn_dict)

        # Set initial rates of neurons:
        if self.params['initial_rates'] is not None:
            # iterate over different initial conditions drawn from a random distribution
            if self.params['initial_rates'] == 'random_uniform':
                gen = self.initial_rates(self.params['initial_rates_iter'],
                                         dim,
                                         mode=self.params['initial_rates'],
                                         rate_max=1000.)
                num_iter = self.params['initial_rates_iter']
            # initial rates are explicitly defined in self.params
            elif isinstance(self.params['initial_rates'], np.ndarray):
                num_iter = 1
                gen = (self.params['initial_rates'] for ii in range(num_iter))
        # if initial rates are not defined, set them 0
        else:
            num_iter = 1
            gen = (np.zeros(dim) for ii in range(num_iter))
        rates = []

        # Loop over all iterations of different initial conditions
        for nsim in range(num_iter):
            print("Iteration {}".format(nsim))
            initial_rates = next(gen)
            for ii in range(dim):
                nest.SetStatus([neurons[ii]], {'rate': initial_rates[ii]})

            # create recording device
            multimeter = nest.Create('multimeter', params={'record_from':
                                                           ['rate'], 'interval': 1.,
                                                           'to_screen': False,
                                                           'to_file': False,
                                                           'to_memory': True})
            # multimeter
            nest.Connect(multimeter, neurons)
            nest.Connect(multimeter, drive)

            # simulate
            nest.Simulate(T)

            data = nest.GetStatus(multimeter)[0]['events']
            res = np.array([np.insert(data['rate'][np.where(data['senders'] == n)],
                                      0,
                                      initial_rates[ii])
                            for ii, n in enumerate(neurons)])

            if full_output:
                rates.append(res)
            else:
                # Keep only initial and final rates
                rates.append(res[:, [0, -1]])

        if num_iter == 1:
            return self.network.structure_vec, rates[0]
        else:
            return self.network.structure_vec, rates

    def integrate_siegert_python(self, full_output=True, parallel=True):
        """
        Integrate siegert formula to obtain stationary rates. See Eq. (3)
        and following in Schuecker, Schmidt et al. (2017).

        Use Runge-Kutta in Python.
        """
        dt = self.params['dt']
        T = self.params['T']
        K = copy(self.network.K_matrix)
        tau = self.NP['tau_m'] * 1e-3
        dim = np.shape(K)[0]

        # Set initial rates of neurons:
        # If defined as None, set them 0
        if self.params['initial_rates'] is None:
            num_iter = 1
            gen = (np.zeros(dim) for ii in range(num_iter))
        # iterate over different initial conditions drawn from a random distribution
        elif self.params['initial_rates'] == 'random_uniform':
                gen = self.initial_rates(self.params['initial_rates_iter'],
                                         dim,
                                         mode=self.params['initial_rates'],
                                         rate_max=1000.)
                num_iter = self.params['initial_rates_iter']
        # initial rates are explicitly defined in self.params
        elif isinstance(self.params['initial_rates'], np.ndarray):
                num_iter = 1
                gen = (self.params['initial_rates'] for ii in range(num_iter))

        rates = []
        # Loop over all iterations of different initial conditions
        for nsim in range(num_iter):
            print("Iteration {}".format(nsim))
            initial_rates = next(gen)

            self.t = np.arange(0, T, dt)
            y = np.zeros((len(self.t), dim), dtype=float)
            y[0, :] = initial_rates

            # Integration loop
            for i, tl in enumerate(self.t[:-1]):
                print(tl)
                delta_y, new_mu, new_sigma = self.Phi(
                    y[i, :], parallel=parallel, return_mu_sigma=True)
                k1 = delta_y
                k2 = self.Phi(
                    y[i, :] + dt * 1e-3 / tau / 2 * k1, parallel=parallel, return_mu_sigma=False)
                k3 = self.Phi(
                    y[i, :] + dt * 1e-3 / tau / 2 * k2, parallel=parallel, return_mu_sigma=False)
                k4 = self.Phi(
                    y[i, :] + dt * 1e-3 / tau * k3, parallel=parallel, return_mu_sigma=False)
                y[i + 1, :] = y[i, :].copy() + dt * 1e-3 / 6. * (k1 + 2 * k2 + 2 * k3 + k4) / tau

            if full_output:
                rates.append(y.T)
            else:
                # Keep only initial and final rates
                rates.append(y.T[:, [0, -1]])

        if num_iter == 1:
            return self.network.structure_vec, rates[0]
        else:
            return self.network.structure_vec, rates

    def nu0_fb(self, arg):
        mu = arg[0]
        sigma = arg[1]
        return nu0_fb(mu, sigma,
                      self.NP['tau_m'] * 1e-3,
                      self.NP['tau_syn'] * 1e-3,
                      self.NP['t_ref'] * 1e-3,
                      self.NP['theta'],
                      self.NP['V_reset'])

    def Phi(self, rates, parallel=False,
            return_mu_sigma=False, return_leak=True):
        mu, sigma = self.mu_sigma(rates, external=True)
        if parallel:
            pool = multiprocessing.Pool(processes=4)
            new_rates = np.array(
                pool.map(self.nu0_fb, zip(mu, sigma)))
            pool.close()
            pool.join()
        else:
            new_rates = [self.nu0_fb((m, s)) for m, s in zip(mu, sigma)]
        if return_leak:
            new_rates -= rates
        if return_mu_sigma:
            return (new_rates), mu, sigma
        else:
            return (new_rates)

    def replace_cc_input(self):
        """
        Helper function to replace cortico-cortical input by different variants.
        """
        mu_CC = np.array([])
        sigma2_CC = np.array([])
        if self.network.params['connection_params']['replace_cc'] == 'het_poisson_stat':
            with open(self.network.params['connection_params'][
                    'replace_cc_input_source'], 'r') as f:
                rates = json.load(f)
                self.cc_input_rates = dict_to_vector(rates,
                                                     self.network.area_list,
                                                     self.network.structure)
        elif self.network.params['connection_params']['replace_cc'] == 'hom_poisson_stat':
            self.cc_input_rates = (np.ones(self.network.K_matrix.shape[0]) *
                                   self.network.params['input_params']['rate_ext'])
        for area in self.network.area_list:
            area_dim = len(self.network.structure[area])
            mask = create_mask(self.network.structure,
                               cortico_cortical=True, target_areas=[area],
                               external=False)

            input_areas = list(set(self.network.structure.keys()).difference({area}))
            rate_mask = create_vector_mask(self.network.structure,
                                           areas=input_areas)
            rate_vector = self.cc_input_rates[rate_mask]
            N_input_pops = self.network.K_matrix.shape[1] - 1 - area_dim
            K_CC = self.network.K_matrix[mask].reshape((area_dim,
                                                        N_input_pops))
            J_CC = self.network.J_matrix[mask].reshape((area_dim,
                                                        N_input_pops))
            mu_CC = np.append(mu_CC, np.dot(K_CC * J_CC, rate_vector))
            sigma2_CC = np.append(sigma2_CC, np.dot(K_CC * J_CC**2, rate_vector))
        tau = self.NP['tau_m'] * 1e-3
        mu_CC *= tau
        sigma2_CC *= tau
        return mu_CC, sigma2_CC

    def initial_rates(self, num_iter, dim, mode='random_uniform', rate_max=100., rng_seed=123):
        """
        Helper function to create generator for initial rates
        """
        np.random.seed(rng_seed)
        n = 0
        while n < num_iter:
            yield rate_max * np.random.rand(dim)
            n += 1

    def mu_sigma(self, rates, external=True, matrix_filter=None,
                 vector_filter=None):
        """
        Calculates mean and variance according to the
        theory.
        """
        if matrix_filter is not None:
            K = copy(self.network.K_matrix)
            J = copy(self.network.J_matrix)
            K[np.logical_not(matrix_filter)] = 0.
            J[np.logical_not(matrix_filter)] = 0.
        else:
            K = self.network.K_matrix
            J = self.network.J_matrix
        if (self.network.params['connection_params']['replace_cc'] in
                ['hom_poisson_stat', 'het_poisson_stat']):
            mu_CC, sigma2_CC = self.replace_cc_input()
            mask = create_mask(self.network.structure, cortico_cortical=True, external=False)
            K[mask] = 0.
        else:
            mu_CC = np.zeros_like(rates)
            sigma2_CC = np.zeros_like(rates)
        KJ = K * J
        J2 = J * J
        if external:
            rates = np.hstack((rates, self.params['input_params']['rate_ext']))
        else:
            rates = np.hstack((rates, np.zeros(self.dim_ext)))
        # if dist:
        #     # due to distributed weights with std = 0.1
        #     J2[:, :7] += 0.01 * J[:, :7] * J[:, :7]
        KJ2 = K * J2
        C_m = self.network.params['neuron_params']['single_neuron_dict']['C_m']
        mu = self.NP['tau_m'] * 1e-3 * np.dot(KJ, rates) + mu_CC + self.NP[
            'tau_m'] / C_m * self.network.add_DC_drive
        sigma2 = self.NP['tau_m'] * 1e-3 * np.dot(KJ2, rates) + sigma2_CC
        sigma = np.sqrt(sigma2)

        return mu, sigma

    def stability_matrix(self, rates, matrix_filter=None,
                         vector_filter=None, full_output=False, replace_cc=None):
        """
        Computes stability matrix on the population level.
        """
        if np.any(matrix_filter is not None):
            assert(np.any(vector_filter is not None))
            assert(self.network.N_vec[vector_filter].size *
                   (self.network.N_vec[vector_filter].size + 1) ==
                   self.network.K_matrix[matrix_filter].size)
            N = self.network.N_vec[vector_filter]
            K = (self.network.K_matrix[matrix_filter].reshape((N.size, N.size + 1)))[:, :-1]
            J = (self.network.J_matrix[matrix_filter].reshape((N.size, N.size + 1)))[:, :-1]
        else:
            N = self.network.N_vec
            K = self.network.K_matrix[:, :-1]
            J = self.network.J_matrix[:, :-1]

        N_pre = np.zeros_like(K)
        N_post = np.zeros_like(K)
        for ii in range(N.size):
            N_pre[ii] = N
            N_post[:, ii] = N

        # Connection probabilities between populations
        C = 1. - (1.-1./(N_pre * N_post))**(K*N_post)
        mu, sigma = self.mu_sigma(rates)

        if np.any(vector_filter is not None):
            mu = mu[vector_filter]
            sigma = sigma[vector_filter]
        slope = np.array([d_nu_d_mu_fb_numeric(1.e-3*self.NP['tau_m'],
                                               1.e-3*self.NP['tau_syn'],
                                               1.e-3*self.NP['t_ref'],
                                               self.NP['V_th'],
                                               self.NP['V_reset'],
                                               mu[ii], sigma[ii]) for ii in range(N.size)])

        # Unit: 1/(mV)**2
        slope_sigma = np.array([d_nu_d_sigma_fb_numeric(1.e-3*self.NP['tau_m'],
                                                        1.e-3*self.NP['tau_syn'],
                                                        1.e-3*self.NP['t_ref'],
                                                        self.NP['V_th'],
                                                        self.NP['V_reset'],
                                                        mu[ii], sigma[ii])*1/(
                                                            2. * sigma[ii])
                                for ii in range(N.size)])

        slope_matrix = np.zeros_like(J)
        slope_sigma_matrix = np.zeros_like(J)
        for ii in range(N.size):
            slope_matrix[:, ii] = slope
            slope_sigma_matrix[:, ii] = slope_sigma
        V = C*(1-C)
        G = (self.NP['tau_m'] * 1e-3)**2 * (slope_matrix*J +
                                            slope_sigma_matrix*J**2)**2
        G_N = N_pre * G
        M = G_N * V
        if full_output:
            return M, slope, slope_sigma, M, C, V, G_N, J, N_pre
        else:
            return M

    def lambda_max(self, rates, matrix_filter=None,
                   vector_filter=None, full_output=False, replace_cc=None):
        """
        Computes radius of eigenvalue spectrum of the stability matrix.
        """
        if full_output:
            (M, slope, slope_sigma,
             M, EV, C, V, G_N) = self.stability_matrix(rates,
                                                       matrix_filter=matrix_filter,
                                                       vector_filter=vector_filter,
                                                       full_output=full_output,
                                                       replace_cc=replace_cc)
        else:
            M = self.stability_matrix(rates, matrix_filter=matrix_filter,
                                      vector_filter=vector_filter,
                                      full_output=full_output, replace_cc=replace_cc)
        EV = np.linalg.eig(M)
        lambda_max = np.sqrt(np.max(np.real(EV[0])))
        if full_output:
            return lambda_max, slope, slope_sigma, M, EV, C, V
        else:
            return lambda_max
