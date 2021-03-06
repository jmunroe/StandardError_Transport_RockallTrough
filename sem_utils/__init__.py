import numpy as np
from scipy import stats
from scipy import optimize

import statsmodels.api as sm
from statsmodels.tsa.ar_model import AutoReg

import pymc3 as pm
from .fixedAutoregressive import fixedAR1

import logging
logger = logging.getLogger('pymc3')
logger.setLevel(logging.ERROR)

##This is for returning 95% CIs:
z95 = 1.959963984540054


####Generating correlated curves:
def gen_correlated_curve(ac, num=100):
    num_with_runup = num+5000
    y = np.zeros((num_with_runup,))
    for i in range(1,num_with_runup):
        y[i] = ac * y[i-1] + np.random.normal()
    return y[-num:]

####This returns the CI based on an SEM that assumes independent data (not correlated data)
def sem_from_independent(timeseries):
    sem = stats.sem(timeseries)
    return sem

def ci_from_independent(timeseries):
    sem = sem_from_independent(timeseries)
    return timeseries.mean()-sem*z95, timeseries.mean()+sem*z95
    

####Block averaging:
def block_averaging(corr):
    sems = list()
    x=np.arange(len(corr))
    for blocksize in range(1, int(len(corr)**(2/3))+1):
        #this is the blocking bit:
        x_ = x[:len(x)-(len(x)%blocksize)].reshape(-1,blocksize).mean(1)
        y_ = corr[:len(x)-(len(x)%blocksize)].reshape(-1,blocksize).mean(1)
        sems.append(stats.sem(y_))
    return sems

def arctan_function(x, a, b, c):
    return a * np.arctan(b*(x-c))

def fit_arctan(blocked_SEMs):
    popt, pcov = optimize.curve_fit(arctan_function, np.arange(len(blocked_SEMs)), np.array(blocked_SEMs), maxfev=20000)
    return popt

def sem_from_blockAveraging(timeseries):
    blocked_sems = block_averaging(timeseries)
    popt = fit_arctan(blocked_sems)
    #asymptote of the arctan curve:
    sem = popt[0] *np.pi/2
    return sem

def ci_from_blockAveraging(timeseries):
    sem = sem_from_blockAveraging(timeseries)
    #now generate a CI:
    return timeseries.mean()-sem*z95, timeseries.mean()+sem*z95


####Techniques that estimate n_eff:
 #first, these are some data processing functions used by the Chodera and Sokal techniques:
def next_pow_two(n):
    i = 1
    while i < n:
        i = i << 1
    return i

def autocorr_func_1d(x):
    n = next_pow_two(len(x))
    # Compute the FFT and then (from that) the auto-correlation function
    f = np.fft.fft(x - np.mean(x), n=2*n)
    acf = np.fft.ifft(f * np.conjugate(f))[:len(x)].real
    acf /= 4*n 
    acf /= acf[0]
    return acf

 # Automated windowing procedure following Sokal (1989)
def auto_window(taus, c):
    m = np.arange(len(taus)) < c * taus
    if np.any(m):
        return np.argmin(m)
    return len(taus) - 1

def sokal_autocorr_time(corr):
    f = autocorr_func_1d(corr)
    taus = 2.0*np.cumsum(f)-1.0
    c=5
    window = auto_window(taus, c)
    return taus[window]

 #this is the Chodera lab 'statistical inefficiency' measure. 
def statistical_inefficiency(corr, mintime=3):
    N = corr.size
    C_t = sm.tsa.stattools.acf(corr, fft=True, adjusted=True, nlags=N)
    t_grid = np.arange(N).astype('float')
    g_t = 2.0 * C_t * (1.0 - t_grid / float(N))
    ind = np.where((C_t <= 0) & (t_grid > mintime))[0][0]
    g = 1.0 + g_t[1:ind].sum()
    return max(1.0, g)

 #and these actually return the values for SEM:
def sem_from_chodera(timeseries):
    autocorrelation_time = statistical_inefficiency(timeseries)
    n = len(timeseries)
    #calculate SEM and return CI:
    sem = np.std(timeseries) / np.sqrt(n/autocorrelation_time)
    return sem

def ci_from_chodera(timeseries):
    sem = sem_from_chodera(timeseries)
    return timeseries.mean()-sem*z95, timeseries.mean()+sem*z95

def sem_from_sokal(timeseries):
    autocorrelation_time = sokal_autocorr_time(timeseries)
    n = len(timeseries)
    #calculate SEM and return a CI:
    sem= np.std(timeseries) / np.sqrt(n/autocorrelation_time)
    return sem

def ci_from_sokal(timeseries):
    sem = sem_from_sokal(timeseries)
    return timeseries.mean()-sem*z95, timeseries.mean()+sem*z95

####Using statsmodels to fit an AR(1) process, thereby estimate rho and use a correction factor:
def correction_factor(rho, n):
    d = ((n-1)*rho - n*rho**2 + rho**(n+1)) / (1-rho)**2
    k = np.sqrt( (1 + (2*d)/n) / ( 1 - (2*d)/(n*(n-1))  ) )
    return k

def sem_from_autoregressive_correction(timeseries):
    fit_result = AutoReg(timeseries-timeseries.mean(), lags = [1]).fit()
    estimated_rho = fit_result.params[1]

    n=len(timeseries)
    correction = correction_factor(estimated_rho, n)
    naive_sem = stats.sem(timeseries)
    #calculate SEM and return CI:
    sem = naive_sem*correction
    return sem

def ci_from_autoregressive_correction(timeseries):
    sem = sem_from_autoregressive_correction(timeseries)
    return timeseries.mean()-sem*z95, timeseries.mean()+sem*z95

#### Bayesian estimation using PyMC3
def bayes_ar_one_model(corr, progress=False):
    with pm.Model() as ar1:
        k_ = pm.Uniform('k',-1,1) #we assume process is stationary, so -1<k_<1 
        tau_ = pm.Gamma('tau',mu=1,sd=1)
        center = pm.Normal('center', mu=corr.mean(), sigma=5) #set the prior for the true mean to be centred on the population mean
        likelihood = fixedAR1('likelihood', k=k_, tau_e=tau_, observed=corr-center)
        trace = pm.sample(progressbar=progress, target_accept=0.9)
    return trace

def hpd_from_bayesian_estimation(timeseries, progress=False):
    trace = bayes_ar_one_model(timeseries, progress=progress)
    hpd = pm.stats.hpd(trace['center'],credible_interval=0.95)
    #this averages the hpd into a SEM. May not be as accurate.
    #hpd_sem = (hpd[1]-hpd[0]/2)/1.96
    #return hpd_sem
    ##this returns the exact HPD:
    return hpd[0], hpd[1]

def sem_from_bayesian_estimation(timeseries, progress=False):
    hpd = hpd_from_bayesian_estimation(timeseries, progress=progress)
    #now try and turn it into a SEM:
    diff = hpd[1]-hpd[0]
    sem = (diff/2)/z95
    return sem



