import numpy as np
from astropy.coordinates import SkyCoord, EarthLocation, AltAz
from astropy.time import Time
from astropy import units as u
from matplotlib import pyplot as plt
import h5py
from tqdm import tqdm as progress_bar
from multiprocessing import Pool, cpu_count

import stan_utility

from ..interfaces.integration import ExposureIntegralTable
from ..interfaces.stan import Direction, convert_scale
from ..interfaces.data import Uhecr
from ..plotting import AllSkyMap
from ..propagation.energy_loss import get_Eth_src, get_kappa_ex, get_Eex, get_Eth_sim, get_arrival_energy, get_arrival_energy_vec
from ..interfaces.utils import get_nucleartable

__all__ = ['Analysis']


class Analysis():
    """
    To manage the running of simulations and fits based on Data and Model objects.
    """

    nthreads = int(cpu_count() * 0.75)

    def __init__(self,
                 data,
                 model,
                 analysis_type=None,
                 filename=None,
                 summary=b''):
        """
        To manage the running of simulations and fits based on Data and Model objects.

        :param data: a Data object
        :param model: a Model object
        :param analysis_type: type of analysis
        """

        self.data = data
        self.model = model
        self.filename = filename

        # Initialise file
        if self.filename:

            with h5py.File(self.filename, 'w') as f:
                desc = f.create_group('description')
                desc.attrs['summary'] = summary

        self.simulation_input = None
        self.fit_input = None

        self.simulation = None
        self.fit = None

        # Simulation outputs
        self.source_labels = None
        self.E = None
        self.Earr = None
        self.Edet = None

        self.arr_dir_type = 'arrival_direction'
        self.E_loss_type = 'energy_loss'
        self.joint_type = 'joint'
        self.gmf_type = "joint_gmf"

        if analysis_type == None:
            analysis_type = self.arr_dir_type

        self.analysis_type = analysis_type

        if self.analysis_type.find('joint') != -1:

            # find lower energy threshold for the simulation, given Eth and Eerr
            self.model.Eth_sim = get_Eth_sim(
                self.data.detector.energy_uncertainty, self.model.Eth)

            # find correspsonding Eth_src
            self.Eth_src = get_Eth_src(self.model.Eth_sim,
                                       self.data.source.distance)

        # Set up integral tables
        params = self.data.detector.params
        varpi = self.data.source.unit_vector
        self.tables = ExposureIntegralTable(varpi=varpi, params=params)

        # table containing (A, Z) of each element
        self.nuc_table = get_nucleartable()

    def build_tables(self, num_points=50, sim_only=False, fit_only=False, parallel=True):
        """
        Build the necessary integral tables.
        """

        if sim_only:

            # kappa_true table for simulation
            if self.analysis_type == self.arr_dir_type or self.analysis_type == self.E_loss_type:
                kappa_true = self.model.kappa

            if self.analysis_type == self.joint_type or self.analysis_type == self.gmf_type:
                self.Eex = get_Eex(self.Eth_src, self.model.alpha)
                self.kappa_ex = get_kappa_ex(self.Eex, self.model.B,
                                             self.data.source.distance)
                kappa_true = self.kappa_ex

            if parallel:
                self.tables.build_for_sim_parallel(kappa_true, self.model.alpha,
                                                   self.model.B, self.data.source.distance)
            else:
                self.tables.build_for_sim(kappa_true, self.model.alpha,
                                          self.model.B, self.data.source.distance)

        if fit_only:

            # logarithmically spcaed array with 60% of points between KAPPA_MIN and 100
            kappa_first = np.logspace(np.log(1),
                                      np.log(10),
                                      int(num_points * 0.7),
                                      base=np.e)
            kappa_second = np.logspace(np.log(10),
                                       np.log(100),
                                       int(num_points * 0.2) + 1,
                                       base=np.e)
            kappa_third = np.logspace(np.log(100),
                                      np.log(1000),
                                      int(num_points * 0.1) + 1,
                                      base=np.e)
            kappa = np.concatenate(
                (kappa_first, kappa_second[1:], kappa_third[1:]), axis=0)

            # full table for fit
            if parallel:
                self.tables.build_for_fit_parallel(kappa)
            else:
                self.tables.build_for_fit(kappa)

    def build_energy_table(self, num_points=50, table_file=None, parallel=True):
        """
        Build the energy interpolation tables.
        """

        self.E_grid = np.logspace(np.log(self.model.Eth),
                                  np.log(1.0e4),
                                  num_points,
                                  base=np.e)
        self.Earr_grid = []

        if parallel:

            args_list = [(self.E_grid, d) for d in self.data.source.distance]
            # parallelize for each source distance
            with Pool(self.nthreads) as mpool:
                results = list(progress_bar(
                    mpool.imap(get_arrival_energy_vec, args_list), total=len(args_list),
                    desc='Precomputing energy grids'
                ))

                self.Earr_grid = results

        else:
            for i in progress_bar(range(len(self.data.source.distance)),
                                  desc='Precomputing energy grids'):
                d = self.data.source.distance[i]
                self.Earr_grid.append(
                    [get_arrival_energy(e, d)[0] for e in self.E_grid])

        if table_file:
            with h5py.File(table_file, 'r+') as f:
                E_group = f.create_group('energy')
                E_group.create_dataset('E_grid', data=self.E_grid)
                E_group.create_dataset('Earr_grid', data=self.Earr_grid)

    def build_kappad(self, table_file=None, particle_type="all", args=None):
        '''
        Evaluate spread parameter for GMF for each UHECR dataset. 
        Note that as of now, UHECR dataset label coincides with
        names from fancy/detector. The output is written to table_file,
        which can then be accessed later with analysis.use_tables().

        If all_particles is True, create kappa_d tables for each element given in
        fancy/interfaces/nuclear_table.pkl. 

        All arrival directions are given in terms of galactic coordinates (lon, lat)
        with respect to mpl: lon \in [-pi,pi], lat \in [-pi/2, pi/2]
        '''
        # there must be a better way to do this...
        if args is not None:
            Nrand, gmf, plot_true = args
        else:
            Nrand, gmf, plot_true = 100, "JF12", False

        omega_true = np.zeros((len(self.data.uhecr.coord.galactic.l.rad), 2))
        omega_true[:, 0] = np.pi - self.data.uhecr.coord.galactic.l.rad
        omega_true[:, 1] = self.data.uhecr.coord.galactic.b.rad

        if particle_type == "all":
            kappad_args_list = [(ptype, Nrand, gmf, plot_true)
                         for ptype in list(self.nuc_table.keys())]

            with Pool(self.nthreads) as mpool:
                results = list(progress_bar(
                    mpool.imap(self.data.uhecr.eval_kappad, kappad_args_list), total=len(kappad_args_list),
                    desc='Precomputing kappa_d for each composition'
                ))

            with h5py.File(table_file, 'r+') as f:
                kappad_group = f.create_group('kappa_d')

                for i, ptype in enumerate(list(self.nuc_table.keys())):
                    particle_group = kappad_group.create_group(ptype)
                    particle_group.create_dataset('kappa_d', data=results[i][0])
                    particle_group.create_dataset(
                        'omega_gal', data=results[i][1])
                    particle_group.create_dataset(
                        'omega_rand', data=results[i][2])
                    particle_group.create_dataset(
                        'omega_true', data=omega_true)

        else:
            kappad_args = (particle_type, Nrand, gmf, plot_true)
            kappa_d, omega_rand, omega_gal = self.data.uhecr.eval_kappad(
                kappad_args = kappad_args)

            if table_file:
                with h5py.File(table_file, 'r+') as f:
                    kappad_group = f.create_group('kappa_d')
                    particle_group = kappad_group.create_group(particle_type)
                    particle_group.create_dataset('kappa_d', data=kappa_d)
                    particle_group.create_dataset('omega_gal', data=omega_gal)
                    particle_group.create_dataset(
                        'omega_rand', data=omega_rand)
                    particle_group.create_dataset(
                        'omega_true', data=omega_true)

    def use_tables(self, input_filename, main_only=True):
        """
        Pass in names of integral tables that have already been made.
        Only the main table is read in by default, the simulation table 
        must be recalculated every time the simulation parameters are 
        changed.
        """

        if main_only:
            input_table = ExposureIntegralTable(input_filename=input_filename)
            self.tables.table = input_table.table
            self.tables.kappa = input_table.kappa

            if self.analysis_type.find("joint") != -1:
                with h5py.File(input_filename, 'r') as f:
                    self.E_grid = f['energy/E_grid'][()]
                    self.Earr_grid = f['energy/Earr_grid'][()]

            if self.analysis_type == self.gmf_type:
                self.ptype = self.data.uhecr.ptype
                with h5py.File(input_filename, 'r') as f:
                    self.kappa_d = f['kappa_d'][self.ptype]["kappa_d"][()]

        else:
            self.tables = ExposureIntegralTable(input_filename=input_filename)

    def _get_zenith_angle(self, c_icrs, loc, time):
        """
        Calculate the zenith angle of a known point 
        in ICRS (equatorial coords) for a given 
        location and time.
        """
        c_altaz = c_icrs.transform_to(AltAz(obstime=time, location=loc))
        return (np.pi / 2 - c_altaz.alt.rad)

    def _simulate_zenith_angles(self, start_year=2004):
        """
        Simulate zenith angles for a set of arrival_directions.

        :params: start_year: year in which measurements started.
        """

        if len(self.arrival_direction.d.icrs) == 1:
            c_icrs = self.arrival_direction.d.icrs[0]
        else:
            c_icrs = self.arrival_direction.d.icrs

        time = []
        zenith_angles = []
        stuck = []

        j = 0
        first = True
        for d in c_icrs:
            za = 99
            i = 0
            while (za > self.data.detector.threshold_zenith_angle.rad):
                dt = np.random.exponential(1 / self.N)
                if (first):
                    t = start_year + dt
                else:
                    t = time[-1] + dt
                tdy = Time(t, format='decimalyear')
                za = self._get_zenith_angle(d, self.data.detector.location,
                                            tdy)

                i += 1
                if (i > 100):
                    za = self.data.detector.threshold_zenith_angle.rad
                    stuck.append(1)
            time.append(t)
            first = False
            zenith_angles.append(za)
            j += 1
            #print(j , za)

        if (len(stuck) > 1):
            print('Warning: % of zenith angles stuck is',
                  len(stuck) / len(zenith_angles) * 100)

        return zenith_angles

    def simulate(self, seed=None, Eth_sim=None):
        """
        Run a simulation.

        :param seed: seed for RNG
        :param Eth_sim: the minimun energy simulated
        """

        eps = self.tables.sim_table

        # handle selected sources
        if (self.data.source.N < len(eps)):
            eps = [eps[i] for i in self.data.source.selection]

        # convert scale for sampling
        D = self.data.source.distance
        alpha_T = self.data.detector.alpha_T
        L = self.model.L
        F0 = self.model.F0
        D, alpha_T, eps, F0, L = convert_scale(D, alpha_T, eps, F0, L)

        if self.analysis_type == self.joint_type \
            or self.analysis_type == self.E_loss_type \
                or self.analysis_type == self.gmf_type:
            # find lower energy threshold for the simulation, given Eth and Eerr
            if Eth_sim:
                self.model.Eth_sim = Eth_sim

        # compile inputs from Model and Data
        self.simulation_input = {
            'kappa_d': self.data.detector.kappa_d,
            'Ns': len(self.data.source.distance),
            'varpi': self.data.source.unit_vector,
            'D': D,
            'A': self.data.detector.area,
            'a0': self.data.detector.location.lat.rad,
            'lon': self.data.detector.location.lon.rad,
            'theta_m': self.data.detector.threshold_zenith_angle.rad,
            'alpha_T': alpha_T,
            'eps': eps
        }

        self.simulation_input['L'] = L
        self.simulation_input['F0'] = F0
        self.simulation_input['distance'] = self.data.source.distance

        if self.analysis_type == self.arr_dir_type or self.analysis_type == self.E_loss_type:

            self.simulation_input['kappa'] = self.model.kappa

        if self.analysis_type == self.E_loss_type:

            self.simulation_input['alpha'] = self.model.alpha
            self.simulation_input['Eth'] = self.model.Eth_sim
            self.simulation_input[
                'Eerr'] = self.data.detector.energy_uncertainty

        if self.analysis_type == self.joint_type:

            self.simulation_input['B'] = self.model.B
            self.simulation_input['alpha'] = self.model.alpha
            self.simulation_input['Eth'] = self.model.Eth_sim
            self.simulation_input[
                'Eerr'] = self.data.detector.energy_uncertainty

        if self.analysis_type == self.gmf_type:

            self.simulation_input['B'] = self.model.B
            self.simulation_input['alpha'] = self.model.alpha
            self.simulation_input['Eth'] = self.model.Eth_sim
            self.simulation_input[
                'Eerr'] = self.data.detector.energy_uncertainty

            # get particle type we intialize simulation with
            ptype = self.model.ptype
            A, Z = self.nuc_table[ptype]
            self.simulation_input["Z"] = Z

        try:
            if self.data.source.flux:
                self.simulation_input['flux'] = self.data.source.flux
            else:
                self.simulation_input['flux'] = np.zeros(self.data.source.N)
        except:
            self.simulation_input['flux'] = np.zeros(self.data.source.N)

        # run simulation
        print('Running Stan simulation...')
        self.simulation = self.model.simulation.sampling(
            data=self.simulation_input,
            iter=1,
            chains=1,
            algorithm="Fixed_param",
            seed=seed)

        # extract output
        print('Extracting output...')
        self.Nex_sim = self.simulation.extract(['Nex_sim'])['Nex_sim']
        arrival_direction = self.simulation.extract(['arrival_direction'
                                                     ])['arrival_direction'][0]
        self.source_labels = (
            self.simulation.extract(['lambda'])['lambda'][0] - 1).astype(int)

        if self.analysis_type == self.joint_type \
            or self.analysis_type == self.E_loss_type \
                or self.analysis_type == self.gmf_type:

            self.Edet = self.simulation.extract(['Edet'])['Edet'][0]
            self.Earr = self.simulation.extract(['Earr'])['Earr'][0]
            self.E = self.simulation.extract(['E'])['E'][0]

            # make cut on Eth
            inds = np.where(self.Edet >= self.model.Eth)
            self.Edet = self.Edet[inds]
            arrival_direction = arrival_direction[inds]
            self.source_labels = self.source_labels[inds]

        # convert to Direction object
        self.arrival_direction = Direction(arrival_direction)
        self.N = len(self.arrival_direction.unit_vector)

        # simulate the zenith angles
        print('Simulating zenith angles...')
        self.zenith_angles = self._simulate_zenith_angles(
            self.data.detector.start_year)
        print('Done!')

        # Make uhecr object
        uhecr_properties = {}
        uhecr_properties['label'] = 'sim_uhecr'
        uhecr_properties['N'] = self.N
        uhecr_properties['unit_vector'] = self.arrival_direction.unit_vector
        uhecr_properties['energy'] = self.Edet
        uhecr_properties['zenith_angle'] = self.zenith_angles
        uhecr_properties['A'] = np.tile(self.data.detector.area, self.N)
        uhecr_properties['source_labels'] = self.source_labels

        uhecr_properties["ptype"] = self.model.ptype if self.analysis_type == self.gmf_type else "p"

        new_uhecr = Uhecr()
        new_uhecr.from_properties(uhecr_properties)

        self.data.uhecr = new_uhecr

    def _prepare_fit_inputs(self):
        """
        Gather inputs from Model, Data and IntegrationTables.
        """

        eps_fit = self.tables.table
        kappa_grid = self.tables.kappa
        E_grid = self.E_grid
        Earr_grid = list(self.Earr_grid)

        # KW: due to multiprocessing appending,
        # collapse dimension from (1, 23, 50) -> (23, 50)
        eps_fit.resize(self.Earr_grid.shape)

        # handle selected sources
        if (self.data.source.N < len(eps_fit)):
            eps_fit = [eps_fit[i] for i in self.data.source.selection]
            Earr_grid = [Earr_grid[i] for i in self.data.source.selection]

        # add E interpolation for background component (possible extension with Dbg)
        Earr_grid.append([0 for e in E_grid])

        # convert scale for sampling
        D = self.data.source.distance
        alpha_T = self.data.detector.alpha_T
        D, alpha_T, eps_fit = convert_scale(D, alpha_T, eps_fit)

        # prepare fit inputs
        self.fit_input = {
            'Ns': self.data.source.N,
            'varpi': self.data.source.unit_vector,
            'D': D,
            'N': self.data.uhecr.N,
            'arrival_direction': self.data.uhecr.unit_vector,
            'A': self.data.uhecr.A,
            'alpha_T': alpha_T,
            'Ngrid': len(kappa_grid),
            'eps': eps_fit,
            'kappa_grid': kappa_grid,
            'zenith_angle': self.data.uhecr.zenith_angle
        }

        if self.analysis_type == self.joint_type \
                or self.analysis_type == self.E_loss_type \
                or self.analysis_type == self.gmf_type:

            self.fit_input['Edet'] = self.data.uhecr.energy
            self.fit_input['Eth'] = self.model.Eth
            self.fit_input['Eerr'] = self.data.detector.energy_uncertainty
            self.fit_input['E_grid'] = E_grid
            self.fit_input['Earr_grid'] = Earr_grid

        if self.analysis_type == self.gmf_type:
            _, self.fit_input["Z"] = self.nuc_table[self.ptype]
            self.fit_input["kappa_d"] = self.kappa_d
        else:
            self.fit_input["kappa_d"] = self.data.detector.kappa_d

    def save(self):
        """
        Write the analysis to file.
        """

        with h5py.File(self.filename, 'r+') as f:

            source_handle = f.create_group('source')
            if self.data.source:
                self.data.source.save(source_handle)

            uhecr_handle = f.create_group('uhecr')
            if self.data.uhecr:
                self.data.uhecr.save(uhecr_handle)

            detector_handle = f.create_group('detector')
            if self.data.detector:
                self.data.detector.save(detector_handle)

            model_handle = f.create_group('model')
            if self.model:
                self.model.save(model_handle)

            fit_handle = f.create_group('fit')
            if self.fit:

                # fit inputs
                fit_input_handle = fit_handle.create_group('input')
                for key, value in self.fit_input.items():
                    fit_input_handle.create_dataset(key, data=value)

                # samples
                samples = fit_handle.create_group('samples')
                for key, value in self.chain.items():
                    samples.create_dataset(key, data=value)

    def plot(self, type=None, cmap=None):
        """
        Plot the data associated with the analysis object.

        type == 'arrival direction':
        Plot the arrival directions on a skymap, 
        with a colour scale describing which source 
        the UHECR is from.

        type == 'energy'
        Plot the simulated energy spectrum from the 
        source, to after propagation (arrival) and 
        detection
        """

        # plot style
        if cmap == None:
            cmap = plt.cm.get_cmap('viridis')

        # plot arrival directions by default
        if type == None:
            type == 'arrival_direction'

        if type == 'arrival_direction':

            # figure
            fig, ax = plt.subplots()
            fig.set_size_inches((12, 6))

            # skymap
            skymap = AllSkyMap(projection='hammer', lon_0=0, lat_0=0)

            self.data.source.plot(skymap)
            self.data.detector.draw_exposure_lim(skymap)
            self.data.uhecr.plot(skymap)

            # standard labels and background
            skymap.draw_standard_labels()

            # legend
            ax.legend(frameon=False, bbox_to_anchor=(0.85, 0.85))

        if type == 'energy':

            bins = np.logspace(np.log(self.model.Eth), np.log(1e4), base=np.e)

            fig, ax = plt.subplots()

            if isinstance(self.E, (list, np.ndarray)):
                ax.hist(self.E,
                        bins=bins,
                        alpha=0.7,
                        label=r'$\tilde{E}$',
                        color=cmap(0.0))
            if isinstance(self.Earr, (list, np.ndarray)):
                ax.hist(self.Earr,
                        bins=bins,
                        alpha=0.7,
                        label=r'$E$',
                        color=cmap(0.5))

            ax.hist(self.data.uhecr.energy,
                    bins=bins,
                    alpha=0.7,
                    label=r'$\hat{E}$',
                    color=cmap(1.0))

            ax.set_xscale('log')
            ax.set_yscale('log')
            ax.legend(frameon=False)

    def use_crpropa_data(self, energy, unit_vector):
        """
        Build fit inputs from the UHECR dataset.
        """

        self.N = len(energy)
        self.arrival_direction = Direction(unit_vector)

        # simulate the zenith angles
        print('Simulating zenith angles...')
        self.zenith_angles = self._simulate_zenith_angles()
        print('Done!')

        # Make Uhecr object
        uhecr_properties = {}
        uhecr_properties['label'] = 'sim_uhecr'
        uhecr_properties['N'] = self.N
        uhecr_properties['unit_vector'] = self.arrival_direction.unit_vector
        uhecr_properties['energy'] = energy
        uhecr_properties['zenith_angle'] = self.zenith_angles
        uhecr_properties['A'] = np.tile(self.data.detector.area, self.N)

        new_uhecr = Uhecr()
        new_uhecr.from_properties(uhecr_properties)

        self.data.uhecr = new_uhecr

    def fit_model(self,
                  iterations=1000,
                  chains=4,
                  seed=None,
                  sample_file=None,
                  warmup=None):
        """
        Fit a model.

        :param iterations: number of iterations
        :param chains: number of chains
        :param seed: seed for RNG
        """

        # Prepare fit inputs
        self._prepare_fit_inputs()

        # fit
        self.fit = self.model.model.sampling(data=self.fit_input,
                                             iter=iterations,
                                             chains=chains,
                                             seed=seed,
                                             sample_file=sample_file,
                                             warmup=warmup)

        # Diagnositics
        stan_utility.utils.check_all_diagnostics(self.fit)

        self.chain = self.fit.extract(permuted=True)
        return self.fit
