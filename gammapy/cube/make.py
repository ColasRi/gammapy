# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import absolute_import, division, print_function, unicode_literals
import logging
from astropy.utils.console import ProgressBar
from astropy.nddata.utils import PartialOverlapError
from astropy.coordinates import Angle
from ..maps import Map, WcsGeom
from .counts import fill_map_counts
from .exposure import make_map_exposure_true_energy
from .background import make_map_background_irf, _fov_background_norm

__all__ = [
    'MapMaker',
    'MapMakerObs',
]

log = logging.getLogger(__name__)


class MapMaker(object):
    """Make maps from IACT observations.

    Parameters
    ----------
    geom : `~gammapy.maps.WcsGeom`
        Reference image geometry
    offset_max : `~astropy.coordinates.Angle`
        Maximum offset angle
    exclusion_mask : `~gammapy.maps.Map`
        Exclusion mask
    cutout_mode : {'trim', 'strict'}, optional
        Options for making cutouts, see :func: `~gammapy.maps.WcsNDMap.make_cutout`
        Should be left to the default value 'trim'
        unless you want only fully contained observations to be added to the map
    """

    def __init__(self, geom, offset_max, exclusion_mask=None, cutout_mode="trim"):
        if not isinstance(geom, WcsGeom):
            raise ValueError('MapMaker only works with WcsGeom')

        if geom.is_image:
            raise ValueError('MapMaker only works with geom with an energy axis')

        self.geom = geom

        self.offset_max = Angle(offset_max)

        self.cutout_mode = cutout_mode

        self.maps = {}

        # Some background estimation methods need an exclusion mask.
        if exclusion_mask is not None:
            self.maps['exclusion'] = exclusion_mask

    def run(self, obs_list, selection=None):
        """
        Run MapMaker for a list of observations to create
        stacked counts, exposure and background maps

        Parameters
        --------------
        obs_list : `~gammapy.data.ObservationList`
            List of observations
        selection : list
            List of str, selecting which maps to make.
            Available: 'counts', 'exposure', 'background'
            By default, all maps are made.

        Returns
        -----------
        maps: dict of stacked counts, background and exposure maps.
        """
        selection = _check_selection(selection)

        # Initialise zero-filled maps
        for name in selection:
            unit = 'm2 s' if name == 'exposure' else ''
            self.maps[name] = Map.from_geom(self.geom, unit=unit)

        for obs in ProgressBar(obs_list):
            self._process_obs(obs, selection)

        return self.maps

    def _process_obs(self, obs, selection):
        # Compute cutout geometry and slices to stack results back later
        try:
            # TODO: this is a hack. We should make cutout better.
            # See https://github.com/gammapy/gammapy/issues/1608
            # We should always make a per-obs map that covers the
            # full observation, so that ba
            cutout_map, cutout_slices = Map.from_geom(self.geom).make_cutout(
                obs.pointing_radec, 2 * self.offset_max, mode=self.cutout_mode,
            )
        except PartialOverlapError:
            # TODO: can we silently do the right thing here? Discuss
            log.warning("Observation {} not fully contained in target image. Skipping it.".format(obs.obs_id))
            return

        log.info('Processing observation {}'.format(obs.obs_id))

        # Compute field of view mask on the cutout
        offset = cutout_map.geom.separation(obs.pointing_radec)
        fov_mask = offset >= self.offset_max

        # Only if there is an exclusion mask, make a cutout
        exclusion_mask = self.maps.get('exclusion', None)
        if exclusion_mask is not None:
            exclusion_mask, _ = exclusion_mask.make_cutout(
                obs.pointing_radec, 2 * self.offset_max, mode=self.cutout_mode,
            )

        # Make maps for this observation
        maps_obs = MapMakerObs(
            obs=obs,
            geom=cutout_map.geom,
            fov_mask=fov_mask,
            exclusion_mask=exclusion_mask,
        ).run(selection)

        # Stack observation maps to total
        for name in selection:
            data = maps_obs[name].quantity.to(self.maps[name].unit).value
            self.maps[name].data[cutout_slices] += data


class MapMakerObs(object):
    """Make maps for a single IACT observation.

    Parameters
    ----------
    obs : `~gammapy.data.DataStoreObservation`
        Observation
    geom : `~gammapy.maps.WcsGeom`
        Reference image geometry
    fov_mask : `~numpy.ndarray`
        Mask to select pixels in field of view
    exclusion_mask : `~gammapy.maps.Map`
        Exclusion mask (used by some background estimators)
    """

    def __init__(self, obs, geom, fov_mask=None, exclusion_mask=None):
        self.obs = obs
        self.geom = geom
        self.fov_mask = fov_mask
        self.exclusion_mask = exclusion_mask
        self.maps = {}

    def run(self, selection=None):
        """Make maps.

        Returns dict with keys "counts", "exposure" and "background".

        Parameters
        ----------
        selection : list
            List of str, selecting which maps to make.
            Available: 'counts', 'exposure', 'background'
            By default, all maps are made.
        """
        selection = _check_selection(selection)

        for name in selection:
            getattr(self, '_make_' + name)()

        return self.maps

    def _make_counts(self):
        counts = Map.from_geom(self.geom)
        fill_map_counts(counts, self.obs.events)
        if self.fov_mask is not None:
            counts.data[..., self.fov_mask] = 0
        self.maps['counts'] = counts

    def _make_exposure(self):
        exposure = make_map_exposure_true_energy(
            pointing=self.obs.pointing_radec,
            livetime=self.obs.observation_live_time_duration,
            aeff=self.obs.aeff,
            geom=self.geom,
        )
        if self.fov_mask is not None:
            exposure.data[..., self.fov_mask] = 0
        self.maps['exposure'] = exposure

    def _make_background(self):
        background = make_map_background_irf(
            pointing=self.obs.pointing_radec,
            livetime=self.obs.observation_live_time_duration,
            bkg=self.obs.bkg,
            geom=self.geom,
        )
        if self.fov_mask is not None:
            background.data[..., self.fov_mask] = 0

        # TODO: decide what background modeling options to support
        # This is not well tested or documented at the moment,
        # so for now take this out
        # background_scale = _fov_background_norm(
        #     acceptance_map=background,
        #     counts_map=counts,
        #     exclusion_mask=self.exclusion_mask,
        # )
        # if self.fov_mask is not None:
        #     background.data *= background_scale[:, None, None]
        self.maps['background'] = background


def _check_selection(selection):
    """Handle default and validation of selection"""
    available = ['counts', 'exposure', 'background']

    if selection is None:
        selection = available

    if not isinstance(selection, list):
        raise TypeError('Selection must be a list of str')

    for name in selection:
        if name not in available:
            raise ValueError('Selection not available: {!r}'.format(name))

    return selection