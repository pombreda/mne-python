"""
====================================================================
Mass-univariate twoway repeated measures ANOVA on single trial power
====================================================================

This script shows how to conduct a mass-univariate repeated measures
ANOVA. As the model to be fitted assumes two fully crossed factors,
we will study the interplay between perceptual modality
(auditory VS visual) and the location of stimulus presentation
(left VS right). Here we use single trials as replications
(subjects) while iterating over time slices plus frequency bands
for to fit our mass-univariate model. For the sake of simplicity we
will confine this analysis to one single channel of which we know
that it exposes a strong induced response. We will then visualize
each effect by creating a corresponding mass-univariate effect
image. We conclude with accounting for multiple comparisons by
performing a permutation clustering test using the ANOVA as
clustering function. The results final will be compared to
multiple comparisons using False Discovery Rate correction.
"""
# Authors: Denis Engemann <d.engemann@fz-juelich.de>
#          Eric Larson <larson.eric.d@gmail.com>
#          Alexandre Gramfort <gramfort@nmr.mgh.harvard.edu>
#
# License: BSD (3-clause)

print __doc__

import numpy as np

import mne
from mne import fiff
from mne.time_frequency import single_trial_power
from mne.stats import f_threshold_twoway_rm, f_twoway_rm, fdr_correction
from mne.datasets import sample

###############################################################################
# Set parameters
data_path = sample.data_path()
raw_fname = data_path + '/MEG/sample/sample_audvis_raw.fif'
event_fname = data_path + '/MEG/sample/sample_audvis_raw-eve.fif'
event_id = 1
tmin = -0.2
tmax = 0.5

# Setup for reading the raw data
raw = fiff.Raw(raw_fname)
events = mne.read_events(event_fname)

include = []
raw.info['bads'] += ['MEG 2443']  # bads

# picks MEG gradiometers
picks = fiff.pick_types(raw.info, meg='grad', eeg=False, eog=True,
                        stim=False, include=include, exclude='bads')

ch_name = raw.info['ch_names'][picks[0]]

# Load conditions
reject = dict(grad=4000e-13, eog=150e-6)
event_id = dict(aud_l=1, aud_r=2, vis_l=3, vis_r=4)
epochs = mne.Epochs(raw, events, event_id, tmin, tmax,
                                picks=picks, baseline=(None, 0),
                                reject=reject)

# make sure all conditions have the same counts, as the ANOVA expects a
# fully balanced data matrix and does not forgive imbalances that generously
# (risk of type-I error)
epochs.equalize_event_counts(event_id, copy=False)
# Time vector
times = 1e3 * epochs.times  # change unit to ms

# Factor to downs-sample the temporal dimension of the PSD computed by
# single_trial_power.
decim = 2
frequencies = np.arange(7, 30, 3)  # define frequencies of interest
Fs = raw.info['sfreq']  # sampling in Hz
n_cycles = frequencies / frequencies[0]
baseline_mask = times[::decim] < 0

# now create TFR representations for all conditions
epochs_power = []
for condition in [epochs[k].get_data()[:, 97:98, :] for k in event_id]:
    this_power = single_trial_power(condition, Fs=Fs, frequencies=frequencies,
        n_cycles=n_cycles, use_fft=False, decim=decim)
    this_power = this_power[:, 0, :, :]  # we only have one channel.
    # Compute ratio with baseline power (be sure to correct time vector with
    # decimation factor)
    epochs_baseline = np.mean(this_power[:, :, baseline_mask], axis=2)
    this_power /= epochs_baseline[..., np.newaxis]
    epochs_power.append(this_power)

###############################################################################
# Setup repeated measures ANOVA

n_conditions = len(epochs.event_id)
n_replications = epochs.events.shape[0] / n_conditions
# we will tell the ANOVA how to interpret the data matrix in terms of
# factors. This done via the factor levels argument which is a list
# of the number factor levels for each factor.
factor_levels = [2, 2]  # number of levels in each factor
effects = 'A*B'  # this is the default signature for computing all effects
# Other possible options are 'A' or 'B' for the corresponding main effects
# or 'A:B' for the interaction effect only (this notation is borrowed from the
# R formula language)
n_frequencies = len(frequencies)
n_times = len(times[::decim])

# Now we'll assemble the data matrix and swap axes so the trial replications
# are the first dimension and the conditions are the second dimension
data = np.swapaxes(np.asarray(epochs_power), 1, 0)
# reshape last two dimensions in one mass-univariate observation-vector
data = data.reshape(n_replications, n_conditions, n_frequencies * n_times)

# so we have replications * conditions * observations:
print data.shape

# while the iteration scheme used above for assembling the data matrix
# makes sure the first two dimensions are organized as expected (with A =
# modality and B = location):
#
#           A1B1 A1B2 A2B1 B2B2
# trial 1   1.34 2.53 0.97 1.74
# trial ... .... .... .... ....
# trial 56  2.45 7.90 3.09 4.76
#
# Now we're ready to run our repeated measures ANOVA.

fvals, pvals = f_twoway_rm(data, factor_levels, effects=effects)

effect_labels = ['modality', 'location', 'modality by location']
import pylab as pl

# let's visualize our effects by computing f-images
for effect, sig, effect_label in zip(fvals, pvals, effect_labels):
    pl.figure()
    # show naive F-values in gray
    pl.imshow(effect.reshape(8, 211), cmap=pl.cm.gray, extent=[times[0],
        times[-1], frequencies[0], frequencies[-1]], aspect='auto',
        origin='lower')
    # create mask for significant Time-frequency locations
    effect = np.ma.masked_array(effect, [sig > .05])
    pl.imshow(effect.reshape(8, 211), cmap=pl.cm.jet, extent=[times[0],
        times[-1], frequencies[0], frequencies[-1]], aspect='auto',
        origin='lower')
    pl.colorbar()
    pl.xlabel('time (ms)')
    pl.ylabel('Frequency (Hz)')
    pl.title(r"Time-locked response for '%s' (%s)" % (effect_label, ch_name))
    pl.show()

# Note. As we treat trials as subjects, the test only accounts for
# time locked responses despite the 'induced' approach.
# For analysis for induced power at the group level averaged TRFs
# are required.


###############################################################################
# Account for multiple comparisons using FDR versus permutation clustering test

# First we need to slightly modify the ANOVA function to be suitable for
# the clustering procedure. Also want to set some defaults.
# Let's first override effects to confine the analysis to the interaction
effects = 'A:B'


# A stat_fun must deal with a variable number of input arguments.
def stat_fun(*args):
    # Inside the clustering function each condition will be passed as
    # flattened array, necessitated by the clustering procedure.
    # The ANOVA however expects an input array of dimensions:
    # subjects X conditions X observations (optional).
    # The following expression catches the list input, swaps the first and the
    # second dimension and puts the remaining observations in the third
    # dimension.
    data = np.swapaxes(np.asarray(args), 1, 0).reshape(n_replications, \
        n_conditions, n_times * n_frequencies)
    return f_twoway_rm(data, factor_levels=factor_levels, effects=effects,
                return_pvals=False)[0]
    # The ANOVA returns a tuple f-values and p-values, we will pick the former.


pthresh = 0.00001  # set threshold rather high to save some time
f_thresh = f_threshold_twoway_rm(n_replications, factor_levels, effects,
                                 pthresh)
tail = 1  # f-test, so tail > 0
n_permutations = 256  # Save some time (the test won't be too sensitive ...)
T_obs, clusters, cluster_p_values, h0 = mne.stats.permutation_cluster_test(
    epochs_power, stat_fun=stat_fun, threshold=f_thresh, tail=tail, n_jobs=1,
    n_permutations=n_permutations)

# Create new stats image with only significant clusters
good_clusers = np.where(cluster_p_values < .05)[0]
T_obs_plot = np.ma.masked_array(T_obs, np.invert(clusters[good_clusers]))

pl.figure()
for f_image, cmap in zip([T_obs, T_obs_plot], [pl.cm.gray, pl.cm.jet]):
    pl.imshow(f_image, cmap=cmap, extent=[times[0], times[-1],
          frequencies[0], frequencies[-1]], aspect='auto',
          origin='lower')
pl.xlabel('time (ms)')
pl.ylabel('Frequency (Hz)')
pl.title('Time-locked response for \'modality by location\' (%s)\n'
          ' cluster-level corrected (p <= 0.05)' % ch_name)
pl.show()

# now using FDR
mask, _ = fdr_correction(pvals[2])
T_obs_plot2 = np.ma.masked_array(T_obs, np.invert(mask))

pl.figure()
for f_image, cmap in zip([T_obs, T_obs_plot2], [pl.cm.gray, pl.cm.jet]):
    pl.imshow(f_image, cmap=cmap, extent=[times[0], times[-1],
          frequencies[0], frequencies[-1]], aspect='auto',
          origin='lower')

pl.xlabel('time (ms)')
pl.ylabel('Frequency (Hz)')
pl.title('Time-locked response for \'modality by location\' (%s)\n'
          ' FDR corrected (p <= 0.05)' % ch_name)
pl.show()

# Both, cluster level and FDR correction help getting rid of
# putatively spots we saw in the naive f-images.