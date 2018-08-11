import numpy as np
import pystan
from astropy.coordinates import SkyCoord, EarthLocation, AltAz
from astropy.time import Time
from astropy import units as u
from matplotlib import pyplot as plt

from ..interfaces.integration import ExposureIntegralTable
from ..interfaces.stan import Direction, convert_scale
from ..interfaces import stan_utility
from ..utils import PlotStyle
from ..plotting import AllSkyMap
from ..propagation.energy_loss import get_Eth_src, get_kappa_ex, get_Eex


__all__ = ['Analysis']


class Analysis():
    """
    To manage the running of simulations and fits based on Data and Model objects.
    """

    def __init__(self, data, model, analysis_type = None):
        """
        To manage the running of simulations and fits based on Data and Model objects.
        
        :param data: a Data object
        :param model: a Model object
        :param analysis_type: type of analysis
        """

        self.data = data

        self.model = model

        self.simulation_input = None
        self.fit_input = None
        
        self.simulation = None
        self.fit = None

        self.arr_dir_type = 'arrival direction'
        self.joint_type = 'joint'

        if analysis_type == None:
            analysis_type = self.arr_dir_type
        self.analysis_type = analysis_type

        if self.analysis_type == 'joint':
            self.Eth_src = get_Eth_src(self.model.Eth, self.data.source.distance)
        
    def build_tables(self, sim_table_filename, num_points = None, table_filename = None, sim_only = False):
        """
        Build the necessary integral tables.
        """

        self.sim_table_filename = sim_table_filename
        self.table_filename = table_filename 

        params = self.data.detector.params
        varpi = self.data.source.unit_vector

        if self.analysis_type == self.arr_dir_type:
            kappa_true = self.model.kappa
            
        if self.analysis_type == self.joint_type:
            self.Eex = get_Eex(self.Eth_src, self.model.alpha)
            self.kappa_ex = get_kappa_ex(self.Eex, self.model.B, self.data.source.distance)        
            kappa_true = self.kappa_ex
            
        # kappa_true table for simulation
        self.sim_table_to_build = ExposureIntegralTable(kappa_true, varpi, params, self.sim_table_filename)
        self.sim_table_to_build.build_for_sim()
        self.sim_table = pystan.read_rdump(self.sim_table_filename)
        
        if not sim_only:
            # logarithmically spcaed array with 60% of points between KAPPA_MIN and 100
            kappa_first = np.logspace(np.log(1), np.log(10), int(num_points * 0.7), base = np.e)
            kappa_second = np.logspace(np.log(10), np.log(100), int(num_points * 0.2) + 1, base = np.e)
            kappa_third = np.logspace(np.log(100), np.log(1000), int(num_points * 0.1) + 1, base = np.e)
            kappa = np.concatenate((kappa_first, kappa_second[1:], kappa_third[1:]), axis = 0)
        
            # full table for fit
            self.table_to_build = ExposureIntegralTable(kappa, varpi, params, self.table_filename)
            self.table_to_build.build_for_fit()
            self.table = pystan.read_rdump(self.table_filename)
        

    def use_tables(self, table_filename, sim_table_filename):
        """
        Pass in names of integral tables that have already been made.
        """
        self.sim_table_filename = sim_table_filename
        self.table_filename = table_filename 
        
        self.sim_table = pystan.read_rdump(self.sim_table_filename)
        self.table = pystan.read_rdump(self.table_filename)
        
        
    def _get_zenith_angle(self, c_icrs, loc, time):
        """
        Calculate the zenith angle of a known point 
        in ICRS (equatorial coords) for a given 
        location and time.
        """
        c_altaz = c_icrs.transform_to(AltAz(obstime = time, location = loc))
        return (np.pi/2 - c_altaz.alt.rad)


    def _simulate_zenith_angles(self):
        """
        Simulate zenith angles for a set of arrival_directions.
        """

        start_time = 2004
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
                dt = np.random.exponential(1 / self.Nex_sim)
                if (first):
                    t = start_time + dt
                else:
                    t = time[-1] + dt
                tdy = Time(t, format = 'decimalyear')
                za = self._get_zenith_angle(d, self.data.detector.location, tdy)
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
            print('Warning: % of zenith angles stuck is', len(stuck)/len(zenith_angles) * 100)

        return zenith_angles

    
    def simulate(self, seed = None):
        """
        Run a simulation.

        :param seed: seed for RNG
        """

        eps = self.sim_table['table'][0]

        # handle selected sources
        if (self.data.source.N < len(eps)):
            eps = [eps[i] for i in self.data.source.selection]

        # convert scale for sampling
        D = self.data.source.distance
        alpha_T = self.data.detector.alpha_T
        L = self.model.L
        F0 = self.model.F0
        Dbg = self.model.Dbg
        D, Dbg, alpha_T, eps, F0, L = convert_scale(D, Dbg, alpha_T, eps, F0, L)
            
        # compile inputs from Model and Data
        self.simulation_input = {
                       'kappa_c' : self.data.detector.kappa_c, 
                       'Ns' : len(self.data.source.distance),
                       'varpi' : self.data.source.unit_vector, 
                       'D' : D,
                       'A' : self.data.detector.area,
                       'a0' : self.data.detector.location.lat.rad,
                       'theta_m' : self.data.detector.threshold_zenith_angle.rad, 
                       'alpha_T' : alpha_T,
                       'eps' : eps}

        if self.analysis_type == self.arr_dir_type:

            self.simulation_input['F_T'] = self.model.F_T
            self.simulation_input['f'] = self.model.f
            self.simulation_input['kappa'] = self.model.kappa
            
        if self.analysis_type == self.joint_type:
            
            self.simulation_input['B'] = self.model.B
            
            self.simulation_input['L'] = L
            self.simulation_input['F0'] = F0
            
            self.simulation_input['alpha'] = self.model.alpha
            self.simulation_input['Eth'] = self.model.Eth
            self.simulation_input['Eerr'] = self.model.Eerr

            self.simulation_input['Dbg'] = Dbg
        
        # run simulation
        print('running stan simulation...')
        self.simulation = self.model.simulation.sampling(data = self.simulation_input, iter = 1,
                                                         chains = 1, algorithm = "Fixed_param", seed = seed)

        print('done')

        # extract output
        print('extracting output...')
        self.Nex_sim = self.simulation.extract(['Nex_sim'])['Nex_sim']
        arrival_direction = self.simulation.extract(['arrival_direction'])['arrival_direction'][0]
        self.arrival_direction = Direction(arrival_direction)

        if self.analysis_type == self.joint_type:
            
            self.Edet = self.simulation.extract(['Edet'])['Edet'][0]
            self.Earr = self.simulation.extract(['Earr'])['Earr'][0]
            self.E = self.simulation.extract(['E'])['E'][0]
        
        print('done')

        # simulate the zenith angles
        print('simulating zenith angles...')
        self.zenith_angles = self._simulate_zenith_angles()
        print('done')
        
        eps_fit = self.table['table']
        kappa_grid = self.table['kappa']

        # handle selected sources
        if (self.data.source.N < len(eps_fit)):
            eps_fit = [eps_fit[i] for i in self.data.source.selection]

        # convert scale for sampling
        D = self.data.source.distance
        alpha_T = self.data.detector.alpha_T
        L = self.model.L
        F0 = self.model.F0
        Dbg = self.model.Dbg
        D, Dbg, alpha_T, eps_fit, F0, L = convert_scale(D, Dbg, alpha_T, eps_fit, F0, L)
            
        # prepare fit inputs
        print('preparing fit inputs...')
        self.fit_input = {'Ns' : self.data.source.N, 
                          'varpi' :self.data.source.unit_vector,
                          'D' : D, 
                          'N' : len(self.arrival_direction.unit_vector), 
                          'arrival_direction' : self.arrival_direction.unit_vector, 
                          'A' : np.tile(self.data.detector.area, len(self.arrival_direction.unit_vector)),
                          'kappa_c' : self.data.detector.kappa_c,
                          'alpha_T' : alpha_T, 
                          'Ngrid' : len(kappa_grid), 
                          'eps' : eps_fit, 
                          'kappa_grid' : kappa_grid,
                          'zenith_angle' : self.zenith_angles}

        if self.analysis_type == self.joint_type:
            
            self.fit_input['Edet'] = self.Edet
            self.fit_input['Eth'] = self.model.Eth
            self.fit_input['Eerr'] = self.model.Eerr
            self.fit_input['Dbg'] = Dbg
            self.fit_input['Ltrue'] = L
            
        print('done')
        
        
    def save_simulated_data(self, filename):
        """
        Write the simulated data to file.
        """
        if self.fit_input != None:
            pystan.stan_rdump(self.fit_input, filename)
        else:
            print("Error: nothing to save!")

            
    def plot_simulation(self, type = None, cmap = None):
        """
        Plot the simulated data.
        
        type == 'arrival direction':
        Plot the arrival directions on a skymap, 
        with a colour scale describing which source 
        the UHECR is from (background in black).

        type == 'energy'
        Plot the simulated energy spectrum from the 
        source, to after propagation (arrival) and 
        detection
        """

        # plot arrival directions by default
        if type == None:
            type == 'arrival direction'
        
        if type == 'arrival direction':

            # plot style
            if cmap == None:
                style = PlotStyle()
            else:
                style = PlotStyle(cmap_name = cmap)
            
            # figure
            fig = plt.figure(figsize = (12, 6));
            ax = plt.gca()

            # skymap
            skymap = AllSkyMap(projection = 'hammer', lon_0 = 0, lat_0 = 0);

            self.data.source.plot(style, skymap)
            self.data.detector.draw_exposure_lim(skymap)
       
            labels = (self.simulation.extract(['lambda'])['lambda'][0] - 1).astype(int)

            Ns = self.data.source.N
            cmap = plt.cm.get_cmap('plasma', Ns - 1) 
            label = True
            for lon, lat, lab in np.nditer([self.arrival_direction.lons, self.arrival_direction.lats, labels]):
                if (lab == Ns):
                    color = 'k'
                else:
                    color = cmap(lab)
                if label:
                    skymap.tissot(lon, lat, self.data.uhecr.coord_uncertainty, npts = 30, facecolor = color,
                                  alpha = 0.5, label = 'simulated data')
                    label = False
                else:
                    skymap.tissot(lon, lat, self.data.uhecr.coord_uncertainty, npts = 30, facecolor = color, alpha = 0.5)

            # standard labels and background
            skymap.draw_standard_labels(style.cmap, style.textcolor)

            # legend
            plt.legend(bbox_to_anchor = (0.85, 0.85))
            leg = ax.get_legend()
            frame = leg.get_frame()
            frame.set_linewidth(0)
            frame.set_facecolor('None')
            for text in leg.get_texts():
                plt.setp(text, color = style.textcolor)

        if type == 'energy':

            bins = np.logspace(np.log(self.model.Eth), np.log(1e4), base = np.e)
            plt.hist(self.E, bins = bins, alpha = 0.7, label = 'source')
            plt.hist(self.Earr, bins = bins, alpha = 0.7, label = 'arrival')
            plt.hist(self.Edet, bins = bins, alpha = 0.7, label = 'detection')
            plt.xscale('log')
            plt.yscale('log')
            plt.legend()
    
        
    def use_simulated_data(self, filename):
        """
        Read in simulated data from a file.
        """

        self.fit_input = pystan.read_rdump(filename)

        
    def use_uhecr_data(self):
        """
        Build fit inputs from the UHECR dataset.
        """

        eps_fit = self.table['table']
        kappa_grid = self.table['kappa']

        # handle selected sources
        if (self.data.source.N < len(eps_fit)):
            eps_fit = [eps_fit[i] for i in self.data.source.selection]

        # convert scale for sampling
        D = self.data.source.distance
        alpha_T = self.data.detector.alpha_T
        L = self.model.L
        F0 = self.model.F0
        Dbg = self.model.Dbg
        D, Dbg, alpha_T, eps_fit, F0, L = convert_scale(D, Dbg, alpha_T, eps_fit, F0, L)
                
        print('preparing fit inputs...')
        self.fit_input = {'Ns' : self.data.source.N,
                          'varpi' :self.data.source.unit_vector,
                          'D' : D,
                          'N' : self.data.uhecr.N,
                          'arrival_direction' : self.data.uhecr.unit_vector,
                          'A' : self.data.uhecr.A,
                          'kappa_c' : self.data.detector.kappa_c,
                          'alpha_T' : alpha_T,
                          'Ngrid' : len(kappa_grid),
                          'eps' : eps_fit,
                          'kappa_grid' : kappa_grid,
                          'zenith_angle' : self.data.uhecr.incidence_angle}

        if self.analysis_type == self.joint_type:

            self.fit_input['Edet'] = self.Edet
            self.fit_input['Eth'] = self.model.Eth
            self.fit_input['Eerr'] = self.model.Eerr
            self.fit_input['Dbg'] = Dbg
            self.fit_input['Ltrue'] = L
            
        print('done')

        
    def fit_model(self, iterations = 1000, chains = 4, seed = None):
        """
        Fit a model.

        :param iterations: number of iterations
        :param chains: number of chains
        :param seed: seed for RNG
        """

        # fit
        self.fit = self.model.model.sampling(data = self.fit_input, iter = iterations, chains = chains, seed = seed)

        # Diagnositics
        stan_utility.check_treedepth(self.fit)
        stan_utility.check_div(self.fit)
        stan_utility.check_energy(self.fit)

        return self.fit

    def ppc_input(self, filename):
        """
        Use data from the file provided to proved inputs
        to the ppc check.
        """

        inputs = pystan.read_rdump(filename)
        self.B_fit = inputs['B_fit']
        self.alpha_fit = inputs['alpha_fit']
        self.F0_fit = inputs['F0_fit']
        self.L_fit = inputs['L_fit']

    
    def ppc(self, ppc_table_filename, seed = None):
        """
        Run a posterior predictive check.
        Use the fit parameters to simulate a dataset.
        """

        self.ppc_table_filename = ppc_table_filename
        
        if self.analysis_type == 'arrival direction':
            print('No PPC implemented for arrival direction only analysis :( ')

        if self.analysis_type == 'joint':

            # extract fitted parameters
            chain = self.fit.extract(permuted = True)
            self.B_fit = np.mean(chain['B'])
            self.alpha_fit = np.mean(chain['alpha'])
            self.F0_fit = np.mean(chain['F0'])
            self.L_fit = np.mean(np.transpose(chain['L']), axis = 1)
        
            # calculate eps integral
            print('precomputing exposure integrals...')
            self.Eex = get_Eex(self.Eth_src, self.alpha_fit)
            self.kappa_ex = get_kappa_ex(self.Eex, self.B_fit, self.data.source.distance)        
            kappa_true = self.kappa_ex
            varpi = self.data.source.unit_vector
            params = self.data.detector.params
            self.ppc_table_to_build = ExposureIntegralTable(kappa_true, varpi, params, self.ppc_table_filename)
            self.ppc_table_to_build.build_for_sim()
            self.ppc_table = pystan.read_rdump(self.ppc_table_filename)
            
            eps = self.ppc_table['table'][0]

            # convert scale for sampling
            D = self.data.source.distance
            alpha_T = self.data.detector.alpha_T
            L = self.model.L
            F0 = self.model.F0
            Dbg = self.model.Dbg
            D, Dbg, alpha_T, eps, F0, L = convert_scale(D, Dbg, alpha_T, eps, F0, L)
            
            # compile inputs from Model, Data and self.fit
            self.ppc_input = {
                'kappa_c' : self.data.detector.kappa_c,
                'Ns' : self.data.source.N,
                'varpi' : self.data.source.unit_vector,
                'D' : D,
                'A' : self.data.detector.area,
                'a0' : self.data.detector.location.lat.rad,
                'theta_m' : self.data.detector.threshold_zenith_angle.rad,
                'alpha_T' : alpha_T,
                'eps' : eps}

            self.ppc_input['B'] = self.B_fit
            self.ppc_input['L'] = self.L_fit
            self.ppc_input['F0'] = self.F0_fit
            self.ppc_input['alpha'] = self.alpha_fit
            
            self.ppc_input['Eth'] = self.model.Eth
            self.ppc_input['Eerr'] = self.model.Eerr
            self.ppc_input['Dbg'] = Dbg

            # run simulation
            print('running posterior predictive simulation...')
            self.posterior_predictive = self.model.simulation.sampling(data = self.ppc_input, iter = 1,
                                                                       chains = 1, algorithm = "Fixed_param", seed = seed)
            print('done')

            # extract output
            print('extracting output...')
            arrival_direction = self.posterior_predictive.extract(['arrival_direction'])['arrival_direction'][0]
            self.arrival_direction_pred = Direction(arrival_direction)
            self.Edet_pred = self.posterior_predictive.extract(['Edet'])['Edet'][0]
            print('done')

        return self.arrival_direction_pred, self.Edet_pred
        
    def plot_ppc(self, ppc_type = None, cmap = None, use_sim_data = False):
        """
        Plot the posterior predictive check against the data 
        (or original simulation) for ppc_type == 'arrival direction' 
        or ppc_type == 'energy'.
        """

        if ppc_type == None:
            ppc_type = 'arrival direction'

        if ppc_type == 'arrival direction':

            # plot style
            if cmap == None:
                style = PlotStyle()
            else:
                style = PlotStyle(cmap_name = cmap)
            
            # figure
            fig = plt.figure(figsize = (12, 6));
            ax = plt.gca()

            # skymap
            skymap = AllSkyMap(projection = 'hammer', lon_0 = 0, lat_0 = 0);

            self.data.source.plot(style, skymap)
            self.data.detector.draw_exposure_lim(skymap)
       
            label = True
            if use_sim_data:
                for lon, lat in np.nditer([self.arrival_direction.lons, self.arrival_direction.lats]):
                    if label:
                        skymap.tissot(lon, lat, 4.0, npts = 30, alpha = 0.5, label = 'data')
                        label = False
                    else:
                        skymap.tissot(lon, lat, 4.0, npts = 30, alpha = 0.5)
            else:
                for lon, lat in np.nditer([self.data.uhecr.coord.galactic.l.deg, self.data.uhecr.coord.galactic.b.deg]):
                    if label:
                        skymap.tissot(lon, lat, self.data.uhecr.coord_uncertainty, npts = 30, alpha = 0.5, label = 'data')
                        label = False
                    else:
                        skymap.tissot(lon, lat, self.data.uhecr.coord_uncertainty, npts = 30, alpha = 0.5)
                
            label = True
            for lon, lat in np.nditer([self.arrival_direction_pred.lons, self.arrival_direction_pred.lats]):
                if label: 
                    skymap.tissot(lon, lat, self.data.uhecr.coord_uncertainty, npts = 30, alpha = 0.5,
                                  color = 'g', label = 'predicted')
                    label = False
                else:
                    skymap.tissot(lon, lat, self.data.uhecr.coord_uncertainty, npts = 30, alpha = 0.5, color = 'g')

            # standard labels and background
            skymap.draw_standard_labels(style.cmap, style.textcolor)

            # legend
            plt.legend(bbox_to_anchor = (0.85, 0.85))
            leg = ax.get_legend()
            frame = leg.get_frame()
            frame.set_linewidth(0)
            frame.set_facecolor('None')
            for text in leg.get_texts():
                plt.setp(text, color = style.textcolor)

        if ppc_type == 'energy':

            bins = np.logspace(np.log(self.model.Eth), np.log(1e4), base = np.e)
            plt.hist(self.Edet, bins = bins, alpha = 0.7, label = 'data')
            plt.hist(self.Edet_pred, bins = bins, alpha = 0.7, label = 'predicted')
            plt.xscale('log')
            plt.yscale('log')
            plt.legend()
