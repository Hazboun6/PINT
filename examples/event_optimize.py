import numpy as np
import pint.toa as toa
import pint.models
import pint.fitter as fitter
import matplotlib.pyplot as plt
import astropy.table
import astropy.units as u
import psr_utils as pu
import scipy.optimize as op
import sys, os, copy, fftfit

eventfile, parfile, gaussianfile = sys.argv[1:]
nbins = 128  # for fast correlation to marginalize over pulse phase

maxlike = -9e99
numcalls = 0
nwalkers = 100
nsteps = 1500

def measure_phase(profile, template, rotate_prof=True):
    """
    measure_phase(profile, template):
        Call FFTFIT on the profile and template to determine the
            following parameters: shift,eshift,snr,esnr,b,errb,ngood
            (returned as a tuple).  These are defined as in Taylor's
            talk at the Royal Society.
    """
    c,amp,pha = fftfit.cprof(template)
    pha1 = pha[0]
    if (rotate_prof):
        pha = np.fmod(pha-np.arange(1,len(pha)+1)*pha1,2.0*np.pi)
    shift,eshift,snr,esnr,b,errb,ngood = fftfit.fftfit(profile,amp,pha)
    return shift,eshift,snr,esnr,b,errb,ngood

def profile_likelihood(phs, otherargs):
    """
    A single likelihood calc for matching phases to a template
    """
    xvals, phases, lntemplate = otherargs
    trials = phases.astype(np.float64) + phs
    trials[trials > 1.0] -= 1.0 # ensure that all the phases are within 0-1
    return -(np.interp(trials, xvals, lntemplate, right=lntemplate[0])).sum()

def marginalize_over_phase(phases, template, resolution=1.0/1024,
    fftfit=False, showplot=False, minimize=True, lophs=0.0, hiphs=1.0):
    """
    marginalize_over_phase(phases, template, resolution=1.0/1024,
        fast=True, showplot=False, deltabin=8):
            a pulse profile comprised of combined photon phases.  A maximum
            likelood technique is used.  The shift and the max log likehood
            are returned.
    """
    ltemp = len(template)
    xtemp = np.arange(ltemp) * 1.0/ltemp
    lntemplate = np.log(template)
    if minimize:
        phs, like = marginalize_over_phase(phases, template, resolution=1.0/64,
            minimize=False, showplot=showplot)
        phs = 1.0 - phs / ltemp
        hwidth = 0.05
        lophs = phs - hwidth if phs > hwidth else phs + (1.0 - hwidth)
        hiphs = phs + hwidth if phs < 1.0 - hwidth else phs - (1.0 - hwidth)
        result = op.minimize(profile_likelihood, [phs],
            args=[xtemp, phases, lntemplate], bounds=[[lophs, hiphs]])
        return ltemp - result['x'] * ltemp, -result['fun']
    if fftfit:
        deltabin = 2
        h, x = np.histogram(phases.astype(np.float64), ltemp, range=[0.0, 1.0])
        s,es,snr,esnr,b,errb,ngood = measure_phase(h, template,
            rotate_prof=False)
        # s is in bins based on the template size
        lophs = (ltemp - s - deltabin) / float(ltemp)  # bins below
        if lophs < 0.0:  lophs += 1.0
        hiphs = lophs + 2.0 * deltabin / float(ltemp)  # bins above
    dphss = np.arange(lophs, hiphs, resolution)
    trials = phases.astype(np.float64) + dphss[:,np.newaxis]
    # ensure that all the phases are within 0-1
    trials[trials > 1.0] -= 1.0
    lnlikes = (np.interp(trials, xtemp, np.log(template),
        right=np.log(template[0]))).sum(axis=1)
    if showplot:
        plt.plot(dphss, lnlikes)
        plt.xlabel("Pulse Phase")
        plt.ylabel("Log likelihood")
        plt.show()
    return ltemp - dphss[lnlikes.argmax()]*ltemp, lnlikes.max()

class emcee_fitter(fitter.fitter):

    def __init__(self, toas=None, model=None, template=None):
        self.toas = toas
        self.model_init = model
        self.reset_model()
        self.template = template
        self.fitkeys, self.fitvals, self.fiterrs = self.get_lnprior_vals()
        self.n_fit_params = len(self.fitvals)

    def get_event_phases(self):
        """
        Return pulse phases based on the current model
        """
        phss = self.model.phase(self.toas.table)[1]
        # ensure all postive
        return np.where(phss < 0.0, phss + 1.0, phss)

    def get_lnprior_vals(self, errfact=1.5):
        """
        By default use Gaussian priors on fit params of errfact * TEMPO errors
        """
        fitkeys = [p for p in self.model.params if not
            getattr(self.model,p).frozen]
        fitvals = []
        fiterrs = []
        for p in fitkeys:
            fitvals.append(getattr(self.model, p).value)
            fiterrs.append(getattr(self.model, p).uncertainty * errfact)
            if p in ["RAJ", "DECJ", "T0"]:
                fitvals[-1] = fitvals[-1].value
                if p != "T0":
                    fiterrs[-1] = fiterrs[-1].value
        return fitkeys, np.asarray(fitvals), np.asarray(fiterrs)

    def lnprior(self, theta):
        """
        The log prior (in this case, gaussian based on initial param errors)
        """
        lnsum = 0.0
        for val, mn, sig in zip(theta, self.fitvals, self.fiterrs):
            lnsum += (-np.log(sig * np.sqrt(2.0 * np.pi)) -
                (val-mn)**2.0/(2.0*sig**2.0))
        return lnsum

    def lnposterior(self, theta):
        """
        The log posterior (priors * likelihood)
        """
        global maxlike, numcalls
        self.set_params(dict(zip(self.fitkeys, theta)))
        phases = self.get_event_phases()
        lnlikelihood = marginalize_over_phase(phases, self.template)[1]
        numcalls += 1
        if lnlikelihood > maxlike:
            print "New max: ", lnlikelihood
            for name, val in zip(ftr.fitkeys, theta):
                    print "  %8s: %25.15g" % (name, val)
            maxlike = lnlikelihood
            self.maxlike_fitvals = theta
        if numcalls % (nwalkers * nsteps / 100) == 0:
            print "~%d%% complete" % (numcalls / (nwalkers * nsteps / 100))
        return self.lnprior(theta) + lnlikelihood

    def minimize_func(self, theta):
        """
        Returns -log(likelihood) so that we can use scipy.optimize.minimize
        """
        # first scale the params based on the errors
        ntheta = (theta * self.fiterrs) + self.fitvals
        self.set_params(dict(zip(self.fitkeys, ntheta)))
        phases = self.get_event_phases()
        lnlikelihood = marginalize_over_phase(phases, self.template)[1]
        print lnlikelihood, ntheta
        return -lnlikelihood

    def phaseogram(self, bins=100, rotate=0.0, size=5, alpha=0.25, file=False):
        """
        Make a nice 2-panel phaseogram for the current model
        """
        mjds = self.toas.table['tdbld'].astype(np.float64)
        years = (mjds - 51544.0) / 365.25 + 2000.0
        phss = self.get_event_phases() + rotate
        phss[phss > 1.0] -= 1.0
        fig = plt.figure(figsize=(6,8))
        ax1 = plt.subplot2grid((3, 1), (0, 0))
        ax2 = plt.subplot2grid((3, 1), (1, 0), rowspan=2)
        h, x, p = ax1.hist(np.concatenate((phss, phss+1.0)),
            2*bins, range=[0,2], color='k', histtype='step', fill=False, lw=2)
        ax1.set_xlim([0.0, 2.0]) # show 2 pulses
        ax1.set_ylim([0.0, 1.1*h.max()])
        ax1.set_ylabel("Counts")
        ax1.set_title(self.model.PSR.value)
        ax2.scatter(phss, mjds, s=size, color='k', alpha=alpha)
        ax2.scatter(phss+1.0, mjds, s=size, color='k', alpha=alpha)
        ax2.set_xlim([0.0, 2.0]) # show 2 pulses
        ax2.set_ylim([mjds.min(), mjds.max()])
        ax2.set_ylabel("MJD")
        ax2.get_yaxis().get_major_formatter().set_useOffset(False)
        ax2.get_yaxis().get_major_formatter().set_scientific(False)
        ax2r = ax2.twinx()
        ax2r.set_ylim([years.min(), years.max()])
        ax2r.set_ylabel("Year")
        ax2r.get_yaxis().get_major_formatter().set_useOffset(False)
        ax2r.get_yaxis().get_major_formatter().set_scientific(False)
        ax2.set_xlabel("Pulse Phase")
        plt.tight_layout()
        if file:
            plt.savefig(file)
            plt.close()
        else:
            plt.show()

# TODO: make this properly handle long double
if 1 or not (os.path.isfile(eventfile+".pickle") or
    os.path.isfile(eventfile+".pickle.gz")):
    events = np.fromfile(eventfile, sep=' ')
    ts = toa.TOAs(toalist=[toa.TOA(mjd, obs='Geocenter', scale='tdb')
        for mjd in events])
    ts.filename = eventfile
    ts.compute_TDBs()
    ts.compute_posvels()
    #ts.pickle()
else:
    ts = toa.TOAs(eventfile)

# Note: need to correct barycentric times to geocenter times
# Keep a backup copy in case we screw up
tdbs_bak = copy.copy(ts.table['tdbld'])

# Read in initial model
modelin = pint.models.get_model(parfile)

# Remove the dispersion delay as it is unnecessary
modelin.delay_funcs.remove(modelin.dispersion_delay)

# Save the PM and PX values to put them back in later.  Do convert from
# barycenter to geocenter, we need to use the constant RA, DEC that was
# used to do the barycentering originally
if hasattr(modelin, "PMRA"):
    pmra = modelin.PMRA.value
    modelin.PMRA.value = 0.0
if hasattr(modelin, "PMDEC"):
    pmdec = modelin.PMDEC.value
    modelin.PMDEC.value = 0.0
if hasattr(modelin, "PX"):
    px = modelin.PX.value
    modelin.PX.value = 0.0

# Now remove the SS Roemer and Shapiro delays from the event times
# This makes them true "Geocenter" times
ts.table['tdbld'] += (modelin.solar_system_geometric_delay(ts.table) +
    modelin.solar_system_shapiro_delay(ts.table)) / toa.SECS_PER_DAY

# Now reset the PM and PX values
if hasattr(modelin, "PMRA"): modelin.PMRA.value = pmra
if hasattr(modelin, "PMDEC"): modelin.PMDEC.value = pmdec
if hasattr(modelin, "PX"): modelin.PX.value = px

modelin.PMRA.value, modelin.PMDEC.value, modelin.PX.value = pmra, pmdec, px

# Now load in the gaussian template and normalize it
gtemplate = pu.read_gaussfitfile(gaussianfile, nbins)
gtemplate /= gtemplate.sum()

# Now define the requirements for emcee
ftr = emcee_fitter(ts, modelin, gtemplate)

# Now compute the photon phases and see if we see a pulse
phss = ftr.get_event_phases()
print "Starting pulse likelihood:", marginalize_over_phase(phss, gtemplate,
    minimize=True, showplot=True)[1]
ftr.phaseogram(file=ftr.model.PSR.value+"_pre.png")
ftr.phaseogram()

# Try normal optimization first to see how it goes
result = op.minimize(ftr.minimize_func, np.zeros_like(ftr.fitvals))
newfitvals = np.asarray(result['x']) * ftr.fiterrs + ftr.fitvals
ftr.set_params(dict(zip(ftr.fitkeys, newfitvals)))
ftr.phaseogram()

# Set up the initial conditions for the emcee walkers.  Could use the
# scipy.optimize newfitvals instead if they are much better
ndim = ftr.n_fit_params
#pos = [ftr.fitvals + ftr.fiterrs*np.random.randn(ndim)
pos = [newfitvals + ftr.fiterrs*np.random.randn(ndim)
    for i in range(nwalkers)]

import emcee
sampler = emcee.EnsembleSampler(nwalkers, ndim, ftr.lnposterior)
# The number is the number of points in the chain
sampler.run_mcmc(pos, nsteps)

def chains_to_dict(names, sampler):
    chains = [sampler.chain[:,:,ii].T for ii in range(len(names))]
    return dict(zip(names,chains))

def plot_chains(chain_dict, file=False):
    np = len(chain_dict)
    fig, axes = plt.subplots(np, 1, sharex=True, figsize=(8, 9))
    for ii, name in enumerate(chain_dict.keys()):
        axes[ii].plot(chain_dict[name], color="k", alpha=0.3)
        axes[ii].set_ylabel(name)
    axes[np-1].set_xlabel("Step Number")
    fig.tight_layout()
    if file:
        fig.savefig(file)
        plt.close()
    else:
        plt.show()

chains = chains_to_dict(ftr.fitkeys, sampler)
plot_chains(chains, file=ftr.model.PSR.value+"_chains.png")

# Make the triangle plot.
import triangle
burnin = 200
samples = sampler.chain[:, burnin:, :].reshape((-1, ndim))
fig = triangle.corner(samples, labels=ftr.fitkeys)
fig.savefig(ftr.model.PSR.value+"_triangle.png")

# Print the best MCMC values and ranges
ranges = map(lambda v: (v[1], v[2]-v[1], v[1]-v[0]),
    zip(*np.percentile(samples, [16, 50, 84], axis=0)))
print "Post-MCMC values (50th percentile +/- (16th/84th percentile):"
for name, vals in zip(ftr.fitkeys, ranges):
    print "%8s:"%name, "%25.15g (+ %12.5g  / - %12.5g)"%vals

# Make a phaseogram with the 50th percentile values
ftr.set_params(dict(zip(ftr.fitkeys, np.percentile(samples, 50, axis=0))))
ftr.phaseogram(file=ftr.model.PSR.value+"_post.png")