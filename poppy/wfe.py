"""
Analytic optical element classes to introduce a specified wavefront
error in an OpticalSystem

 * ZernikeWFE
 * ParameterizedWFE (for use with hexike or zernike basis functions)
 * SineWaveWFE
 * TODO: MultiSineWaveWFE ?
 * TODO: PowerSpectrumWFE
 * TODO: KolmogorovWFE

"""

import collections
from functools import wraps
import numpy as np
import astropy.units as u

from .optics import AnalyticOpticalElement, CircularAperture
from .poppy_core import Wavefront, PlaneType, BaseWavefront
from poppy.fresnel import FresnelWavefront

from . import zernike
from . import utils
from . import accel_math

__all__ = ['WavefrontError', 'ParameterizedWFE', 'ZernikeWFE', 'SineWaveWFE',
        'StatisticalPSDWFE', 'PowerSpectrumWFE', 'KolmogorovWFE']


def _check_wavefront_arg(f):
    """Decorator that ensures the first positional method argument
    is a poppy.Wavefront or FresnelWavefront
    """

    @wraps(f)
    def wrapper(*args, **kwargs):
        if not isinstance(args[1], BaseWavefront):
            raise ValueError("The first argument must be a Wavefront or FresnelWavefront object.")
        else:
            return f(*args, **kwargs)
    return wrapper


class WavefrontError(AnalyticOpticalElement):
    """A base class for different sources of wavefront error

    Analytic optical elements that represent wavefront error should
    derive from this class and override methods appropriately.
    Defined to be a pupil-plane optic.
    """

    def __init__(self, **kwargs):
        if 'planetype' not in kwargs:
            kwargs['planetype'] = PlaneType.pupil
        super(WavefrontError, self).__init__(**kwargs)
        # in general we will want to see phase rather than intensity at this plane
        self.wavefront_display_hint = 'phase'

    @_check_wavefront_arg
    def get_opd(self, wave):
        """Construct the optical path difference array for a wavefront error source
        as evaluated across the pupil for an input wavefront `wave`

        Parameters
        ----------
        wave : Wavefront
            Wavefront object with a `coordinates` method that returns (y, x)
            coordinate arrays in meters in the pupil plane
        """
        raise NotImplementedError('Not implemented yet')

    def rms(self):
        """RMS wavefront error induced by this surface"""
        raise NotImplementedError('Not implemented yet')

    def peaktovalley(self):
        """Peak-to-valley wavefront error induced by this surface"""
        raise NotImplementedError('Not implemented yet')


def _wave_y_x_to_rho_theta(y, x, pupil_radius):
    """
    Return wave coordinates in (rho, theta) for a Wavefront object
    normalized such that rho == 1.0 at the pupil radius

    Parameters
    ----------
    wave : Wavefront
        Wavefront object with a `coordinates` method that returns (y, x)
        coordinate arrays in meters in the pupil plane
    pupil_radius : float
        Radius (in meters) of a circle circumscribing the pupil.
    """

    if accel_math._USE_NUMEXPR:
        rho = accel_math.ne.evaluate("sqrt(x**2+y**2)/pupil_radius")
        theta = accel_math.ne.evaluate("arctan2(y / pupil_radius, x / pupil_radius)")
    else:
        rho = np.sqrt(x ** 2 + y ** 2) / pupil_radius
        theta = np.arctan2(y / pupil_radius, x / pupil_radius)
    return rho, theta


class ParameterizedWFE(WavefrontError):
    """
    Define an optical element in terms of its distortion as decomposed
    into a set of orthonormal basis functions (e.g. Zernikes,
    Hexikes, etc.). Included basis functions are normalized such that
    user-provided coefficients correspond to meters RMS wavefront
    aberration for that basis function.

    Parameters
    ----------
    coefficients : iterable of numbers
        The contribution of each term to the final distortion, in meters
        RMS wavefront error. The coefficients are interpreted as indices
        in the order of Noll et al. 1976: the first term corresponds to
        j=1, second to j=2, and so on.
    radius : float
        Pupil radius, in meters. Defines the region of the input
        wavefront array over which the distortion terms will be
        evaluated. For non-circular pupils, this should be the circle
        circumscribing the actual pupil shape.
    basis_factory : callable
        basis_factory will be called with the arguments `nterms`, `rho`,
        `theta`, and `outside`.

        `nterms` specifies how many terms to compute, starting with the
        j=1 term in the Noll indexing convention for `nterms` = 1 and
        counting up.

        `rho` and `theta` are square arrays holding the rho and theta
        coordinates at each pixel in the pupil plane. `rho` is
        normalized such that `rho` == 1.0 for pixels at `radius` meters
        from the center.

        `outside` contains the value to assign pixels outside the
        radius `rho` == 1.0. (Always 0.0, but provided for
        compatibility with `zernike.zernike_basis` and
        `zernike.hexike_basis`.)
    """

    @utils.quantity_input(coefficients=u.meter, radius=u.meter)
    def __init__(self, name="Parameterized Distortion", coefficients=None, radius=1*u.meter,
                 basis_factory=None, **kwargs):
        if not isinstance(basis_factory, collections.Callable):
            raise ValueError("'basis_factory' must be a callable that can "
                             "calculate basis functions")
        self.radius = radius
        self.coefficients = coefficients
        self.basis_factory = basis_factory
        self._default_display_size = radius * 3
        super(ParameterizedWFE, self).__init__(name=name, **kwargs)

    @_check_wavefront_arg
    def get_opd(self, wave):
        y, x = self.get_coordinates(wave)
        rho, theta = _wave_y_x_to_rho_theta(y, x, self.radius.to(u.meter).value)

        combined_distortion = np.zeros(rho.shape)

        nterms = len(self.coefficients)
        computed_terms = self.basis_factory(nterms=nterms, rho=rho, theta=theta, outside=0.0)

        for idx, coefficient in enumerate(self.coefficients):
            if coefficient == 0.0:
                continue  # save the trouble of a multiply-and-add of zeros
            coefficient_in_m = coefficient.to(u.meter).value
            combined_distortion += coefficient_in_m * computed_terms[idx]
        return combined_distortion


class ZernikeWFE(WavefrontError):
    """
    Define an optical element in terms of its Zernike components by
    providing coefficients for each Zernike term contributing to the
    analytic optical element.

    Parameters
    ----------
    coefficients : iterable of floats
        Specifies the coefficients for the Zernike terms, ordered
        according to the convention of Noll et al. JOSA 1976. The
        coefficient is in meters of optical path difference (not waves).
    radius : float
        Pupil radius, in meters, over which the Zernike terms should be
        computed such that rho = 1 at r = `radius`.
    """

    @utils.quantity_input(coefficients=u.meter, radius=u.meter)
    def __init__(self, name="Zernike WFE", coefficients=None, radius=None,
            aperture_stop=False, **kwargs):

        if radius is None:
            raise ValueError("You must specify a radius for the unit circle "
                             "over which the Zernike polynomials are normalized")
        self.radius = radius
        self.aperture_stop = aperture_stop
        self.coefficients = coefficients
        self.circular_aperture = CircularAperture(radius=self.radius, gray_pixel=False, **kwargs)
        self._default_display_size = radius * 3
        kwargs.update({'name': name})
        super(ZernikeWFE, self).__init__(**kwargs)

    @_check_wavefront_arg
    def get_opd(self, wave):
        """
        Parameters
        ----------
        wave : poppy.Wavefront (or float)
            Incoming Wavefront before this optic to set wavelength and
            scale, or a float giving the wavelength in meters
            for a temporary Wavefront used to compute the OPD.
        """

        # the Zernike optic, being normalized on a circle, is
        # implicitly also a circular aperture:
        aperture_intensity = self.circular_aperture.get_transmission(wave)

        pixelscale_m = wave.pixelscale.to(u.meter / u.pixel).value

        # whether we can use pre-cached zernikes for speed depends on whether
        # there are any coord offsets. See #229
        has_offset_coords = (hasattr(self, "shift_x") or hasattr(self, "shift_y")
                             or hasattr(self, "rotation"))
        if has_offset_coords:
            y, x = self.get_coordinates(wave)
            rho, theta = _wave_y_x_to_rho_theta(y, x, self.radius.to(u.meter).value)

        combined_zernikes = np.zeros(wave.shape, dtype=np.float64)
        for j, k in enumerate(self.coefficients, start=1):
            k_in_m = k.to(u.meter).value

            if has_offset_coords:
                combined_zernikes += k_in_m * zernike.zernike1(
                    j,
                    rho=rho,
                    theta=theta,
                    outside=0.0,
                    noll_normalize=True
                )
            else:
                combined_zernikes += k_in_m * zernike.cached_zernike1(
                    j,
                    wave.shape,
                    pixelscale_m,
                    self.radius.to(u.meter).value,
                    outside=0.0,
                    noll_normalize=True
                )

        combined_zernikes[aperture_intensity==0] = 0
        return combined_zernikes


    def get_transmission(self, wave):
        if self.aperture_stop:
            return self.circular_aperture.get_transmission(wave)
        else:
            return np.ones(wave.shape)


class SineWaveWFE(WavefrontError):
    """ A single sine wave ripple across the optic

    Specified as a a spatial frequency in cycles per meter, an optional phase offset in cycles,
    and an amplitude.

    By default the wave is oriented in the X direction.
    Like any AnalyticOpticalElement class, you can also specify a rotation parameter to
    rotate the direction of the sine wave.


    (N.b. we intentionally avoid letting users specify this in terms of a spatial wavelength
    because that would risk potential ambiguity with the wavelength of light.)
    """

    @utils.quantity_input(spatialfreq=1. / u.meter, amplitude=u.meter)
    def __init__(self, name='Sine WFE', spatialfreq=1.0, amplitude=1e-6, phaseoffset=0, **kwargs):
        super(WavefrontError, self).__init__(name=name, **kwargs)

        self.sine_spatial_freq = spatialfreq
        self.sine_phase_offset = phaseoffset
        # note, can't call this next one 'amplitude' since that's already a property
        self.sine_amplitude = amplitude

    @_check_wavefront_arg
    def get_opd(self, wave):
        """
        Parameters
        ----------
        wave : poppy.Wavefront (or float)
            Incoming Wavefront before this optic to set wavelength and
            scale, or a float giving the wavelength in meters
            for a temporary Wavefront used to compute the OPD.
        """

        y, x = self.get_coordinates(wave)  # in meters

        opd = self.sine_amplitude.to(u.meter).value * \
              np.sin(2 * np.pi * (x * self.sine_spatial_freq.to(1 / u.meter).value + self.sine_phase_offset))

        return opd


class StatisticalPSDWFE(WavefrontError):
    """
    Statistical PSD WFE class from power law for optical noise.

    Parameters
    ----------
    name : string
        name of the optic
    index: float
        negative power law spectra index, defaults to 3
    wfe: astropy quantity
        wfe in linear astropy units, defaults to 50 nm
    radius: astropy quantity
        radius of optic in linear astropy units, defaults to 1 m
    seed : integer
        seed for the random phase screen generator
    """

    @utils.quantity_input(wfe=u.nm, radius=u.meter)
    def __init__(self, name='PSD WFE', index=3.0, wfe=50*u.nm, radius=1*u.meter, seed=None, **kwargs):

        super().__init__(name=name, **kwargs)
        self.index = index
        self.wfe = wfe
        self.radius = radius
        self.seed = seed

    @_check_wavefront_arg
    def get_opd(self, wave):
        """
        Parameters
        ----------
        wave : poppy.Wavefront (or float)
            Incoming Wavefront before this optic to set wavelength and
            scale, or a float giving the wavelength in meters
            for a temporary Wavefront used to compute the OPD.
        """
        y, x = self.get_coordinates(wave)
        rho, theta = _wave_y_x_to_rho_theta(y, x, self.radius.to(u.meter).value)
        psd = np.power(rho, -self.index)   # generate power-law PSD

        psd_random_state = np.random.RandomState()
        psd_random_state.seed(self.seed)   # if provided, set a seed for random number generator
        rndm_phase = psd_random_state.normal(size=(len(y), len(x)))   # generate random phase screen
        rndm_psd = np.fft.fftshift(np.fft.fft2(np.fft.fftshift(rndm_phase)))   # FT of random phase screen to get random PSD
        scaled = np.sqrt(psd) * rndm_psd    # scale random PSD by power-law PSD
        phase_screen = np.fft.ifftshift(np.fft.ifft2(np.fft.ifftshift(scaled))).real   # FT of scaled random PSD makes phase screen

        phase_screen -= np.mean(phase_screen)  # force zero-mean
        self.opd = phase_screen / np.std(phase_screen) * self.wfe.to(u.m).value  # normalize to wanted input rms wfe

        return self.opd


class PowerSpectrumWFE(WavefrontError):
    r"""
    WFE model specificed via a Power Spectral Density (PSD),
    or a list of multiple PSDs, which follow von Karman PSD model:
    
    :math:`P(k) = \frac{\beta} {\left( \left(\frac{1}{L_{0}}\right)^{2} + |k|^{2} \right)^{{\alpha/2}}} e^{-(|k|l_{0})^{2}} + \beta_{sr}`
    
    where:
    P: astropy quantity
        Power Spectral Density at a spatial frequency.
        Units: :math: `m^{2}m^{2}`
        Assumes surface units of meters (first :math: `m^{2}`)
    k: astropy quantity
        Spatial frequency value, units 1/m
    :math:`\alpha`: float 
        The PSD index value 
    :math:`\beta`: astropy quantity
        The normalization constant. In units of :math: `\frac{m^{2}}{m^{\alpha-2}}`
        Numerator assumes surface units of meters
        Denominator assumes spatial frequency units are 1/m
    :math:`L_{0}`: astropy quantity
        The outer scale value, where the low spatial frequency flattens. Units: m
    :math:`l_{0}`: float
        Inner scale value, where the high spatial frequency flattens.
    :math:`\beta_{sr}`: astropy quantity
        Surface roughness normalization. Should match units of PSD.
    
    References:
    Males, Jared. MagAO-X Preliminary-Design Review, 
        Section 5.1: Optics Specifications, Eqn 1
        https://magao-x.org/docs/handbook/appendices/pdr/
    Lumbres, et al. In Prep.

    Parameters
    ----------
    name : string
        name of the optic
    psd_parameters: list (for single PSD set) or list of lists (multiple PSDs)
        List of specified PSD parameters.
        If there are multiple PSDs, then each list element is a list of specified PSD parameters.
        i.e. [ [PSD_list_0], [PSD_list_1]]
        The PSD parameters in a list are ordered as follows:
        [alpha, beta, outer_scale, inner_scale, surf_roughness]
        where:            
            alpha: float 
                The PSD index value.
            beta: astropy quantity
                The normalization constant. In units of :math: `\frac{m^{2}}{m^{\alpha-2}}`
                Numerator assumes surface units of meters
                Denominator assumes spatial frequency units are 1/m
            outer_scale: astropy quantity
                The outer scale value, where the low spatial frequency flattens. 
                Unit requirement: meters
            inner_scale: float
                Inner scale value, where the high spatial frequency flattens.
            surf_roughness: astropy quantity
                Surface roughness normalization. Should match units of PSD.
    psd_weight: iterable list of floats
        Specifies the weight muliplier to set onto each model PSD
    seed : integer
        Seed for the random phase screen generator
    apply_reflection: boolean
        Applies 2x scale for the OPD as needed for reflection.
        Default to False. 
        Set to True if the PSD model only accounts for surface.
    screen_size: integer
        Sets how large the PSD matrix will be calculated.
        The PSD matrix needs to be larger than the wavefront for Fourier transform padding purposes.
        If None passed in, then code will default size to 4x wavefront's side.
        Default to None.
    rms: astropy quantity
        Optional. Use this to force the wfe RMS
        If a value is passed in, this is the surface rms value (not OPD) in meters.
        If None passed, then the wfe RMS produced is what shows up in PSD calculation.
        Default to None.
    incident_angle: astropy quantity
        Adjusts the WFE based on reflected beam distortion.
        Does not distort the beam (remains circular), but will get the rms equivalent value.
        Can be passed as either degrees or radians.
        Default is 0 degrees (paraxial).
    radius: astropy quantity
        Optional. However, mandatory if rms parameter is passed.
        If a value is passed in, this is the beam radius value for calculating
        the generated WFE rms to compare with the normalized rms value.
        Default to None.
    """

    @utils.quantity_input(rms=u.nm, radius=u.meter, incident_angle=u.deg)
    def __init__(self, name='Model PSD WFE', psd_parameters=None, psd_weight=None, 
                 seed=None, apply_reflection=False, screen_size=None, rms=None,
                 incident_angle=0*u.deg, radius=None, **kwargs):

        super().__init__(name=name, **kwargs)
        self.psd_parameters = psd_parameters
        self.seed = seed
        self.apply_reflection = apply_reflection
        self.screen_size = screen_size
        self.rms = rms
        
        if self.rms is not None and radius is None:
            raise ValueError("You must specify a radius for rms normalization.")
        self.radius = radius
        
        # check incident angle units
        if incident_angle >= 90*u.deg:
            raise ValueError("Incident angle must be less than 90 degrees, or equivalent in other units.")
        self.incident_angle = incident_angle
            
        if psd_weight is None:
            self.psd_weight = np.ones((len(psd_parameters))) # default to equal weights
        else:
            self.psd_weight = psd_weight
        

    @_check_wavefront_arg
    def get_opd(self, wave):
        """
        Parameters
        ----------
        wave : poppy.Wavefront (or float)
            Incoming Wavefront before this optic to set wavelength and
            scale, or a float giving the wavelength in meters
            for a temporary Wavefront used to compute the OPD.
        """
        
        # check that screen size is at least larger than wavefront size
        wave_size = wave.shape[0]
        if wave.ispadded is True: # get true wave size if padded to oversample.
            wave_size = int(wave_size/wave.oversample)
        
        # check that screen size exists
        if self.screen_size is None:
            self.screen_size = wave.shape[0]
            
            if wave.ispadded is False: # sometimes the wave is not padded.
                self.screen_size = self.screen_size * 4 # default 4x, open for discussion
        
        elif self.screen_size < wave_size:
            raise Exception('PSD screen size smaller than wavefront size, recommend at least 2x larger')
        
        # get pixelscale to calculate spatial frequency spacing
        dk = 1/(self.screen_size * wave.pixelscale * u.pix) # eliminate the pixel units
        
        # build spatial frequency map
        cen = int(self.screen_size/2)
        maskY, maskX = np.mgrid[-cen:cen, -cen:cen]
        ky = maskY*dk.to_value(1./u.m)
        kx = maskX*dk.to_value(1./u.m)
        k_map = np.sqrt(kx**2 + ky**2) # unitless for the math, but actually 1/m
        
        # calculate the PSD
        psd = np.zeros_like(k_map) # initialize the total PSD matrix
        for n in range(0, len(self.psd_weight)):
            # loop-internal localized PSD variables
            alpha = self.psd_parameters[n][0]
            beta = self.psd_parameters[n][1]
            outer_scale = self.psd_parameters[n][2]
            inner_scale = self.psd_parameters[n][3]
            surf_roughness = self.psd_parameters[n][4]
            
            # unit check
            psd_units = beta.unit / ((dk.unit**2)**(alpha/2))
            assert surf_roughness.unit == psd_units, "PSD parameter units are not consistent, please re-evaluate parameters."
            surf_unit = (psd_units*(dk.unit**2))**(0.5)
            
            # initialize loop-internal PSD matrix
            psd_local = np.zeros_like(psd)
            
            # Calculate the PSD equation denominator based on outer_scale presence
            if outer_scale.value == 0: # skip out or else PSD explodes
                # temporary overwrite of k_map at k=0 to stop div/0 problem
                k_map[cen][cen] = 1*dk.value
                # calculate PSD as normal
                psd_denom = (k_map**2)**(alpha/2)
                # calculate the immediate PSD value
                psd_interm = (beta.value*np.exp(-((k_map*inner_scale)**2))/psd_denom)
                # overwrite PSD at k=0 to be 0 instead of the original infinity
                psd_interm[cen][cen] = 0
                # return k_map to original state
                k_map[cen][cen] = 0
            else:
                psd_denom = ((outer_scale.value**(-2)) + (k_map**2))**(alpha/2) # unitless currently
                psd_interm = (beta.value*np.exp(-((k_map*inner_scale)**2))/psd_denom)
            
            # apply surface roughness
            psd_interm = psd_interm + surf_roughness.value
            
            # apply as the sum with the weight of the PSD model
            psd = psd + (self.psd_weight[n] * psd_interm) # this should all be m2 [surf_unit]2, but stay unitless for all calculations
        
        # set the random noise
        psd_random = np.random.RandomState()
        psd_random.seed(self.seed)
        rndm_noise = np.fft.fftshift(np.fft.fft2(psd_random.normal(size=(self.screen_size, self.screen_size))))
        
        psd_scaled = (np.sqrt(psd/(wave.pixelscale.value**2)) * rndm_noise)
        opd = ((np.fft.ifft2(np.fft.ifftshift(psd_scaled)).real*surf_unit).to(u.m)).value 
        
        # Set rms value based on the active region of beam
        if self.rms is not None:
            circ = CircularAperture(name='beam diameter', radius=self.radius)
            ap = circ.get_transmission(wave)
            opd_crop = utils.pad_or_crop_to_shape(array=opd, target_shape=wave.shape)
            active_ap = opd_crop[ap==True]
            rms_measure = np.sqrt(np.mean(np.square(active_ap))) # measured rms from aperture
            opd *= self.rms.to(u.m).value/rms_measure # appropriately scales entire OPD
            
        # apply the angle adjustment for rms
        if self.incident_angle.value != 0:
            opd /= np.cos(self.incident_angle).value
        
        # Set reflection OPD
        if self.apply_reflection == True:
            opd *= 2
            
        # Resize PSD screen to shape of wavefront
        if self.screen_size > wave.shape[0]: # crop to wave shape if needed
            opd = utils.pad_or_crop_to_shape(array=opd, target_shape=wave.shape)
        
        self.opd = opd
        return self.opd


class KolmogorovWFE(WavefrontError):
    """ A turbulent phase screen.
    
    This is an implementation of a turbulent phase screen as by the
    Kolmogorov theory of turbulence.
    
    Parameters
    -----------------
    r0 : astropy.quantity
        Fried parameter (m).
    
    Cn2 : astropy.quantity
        Index-of-refraction structure constant (m^{-2/3}).
    
    dz : astropy.quantity
        Propagation distance (m).
    
    inner_scale : astropy.quantity
        Inner scale of the turbulence (m). The inner scale affects the
        calculation results if kind = 'von Karman', 'Tatarski', or 'Hill'.
    
    outer_scale : astropy.quantity
        Outer scale of the turbulence (m). The outer scale only affects the
        calculation results if kind='von Karman'.
    
    kind : string
        Kind of the spatial power spectrum. Must be one of 'Kolmogorov',
        'Tatarski', 'von Karman', 'Hill'.
    
    seed : integer
        Seed for the random number generator when creating the phase screen.
        This can be helpful when multiple fields (for example different modes)
        should propagate through an identical atmosphere.
    
    References
    -------------------
    For a general overview of the Kolmogorov theory, read
    L. C. Andrews and R. L. Phillips, Laser Beam Propagation Through Random
    Media, 2nd ed. (Society of Photo Optical, 2005).
    
    Other relevant references are mentioned in the respective functions.
    """
    
    @utils.quantity_input(r0=u.meter, Cn2=u.meter**(-2/3), dz=u.meter,
                          inner_scale=u.meter, outer_scale=u.meter)
    def __init__(self, name="Kolmogorov WFE", r0=None, Cn2=None, dz=None,
                 inner_scale=None, outer_scale=None, kind='Kolmogorov',
                 seed=None, **kwargs):
        
        if dz is None and not all(item is not None for item in [r0, Cn2]):
            raise ValueError('To prepare a turbulent phase screen, dz and either Cn2 or r0 must be given.')
        
        super(KolmogorovWFE, self).__init__(name=name, **kwargs)
        
        self.r0 = r0
        self.Cn2 = Cn2
        self.seed = seed
        self.dz = dz.to(u.m)
        self.inner_scale = inner_scale
        self.outer_scale = outer_scale
        self.kind = kind
    
    def get_opd(self, wave):
        """ Returns an optical path difference for a turbulent phase screen.
        
        Parameters
        -----------------
        wave : wavefront object
            Wavefront to calculate the phase screen for.
    
        References
        -------------------
        J. A. Fleck Jr, J. R. Morris, and M. D. Feit, Appl. Phys. 10, 129 (1976).
        
        E. M. Johansson and D. T. Gavel,
        in Proc. SPIE, edited by J. B. Breckinridge
        (International Society for Optics and Photonics, 1994), pp. 372–383.
        
        B. J. Herman and L. A. Strugala, in Proc. SPIE,
        edited by P. B. Ulrich and L. E. Wilson
        (International Society for Optics and Photonics, 1990), pp. 183–192.
        
        G. Gbur, J. Opt. Soc. Am. A 31, 2038 (2014).
        
        D. L. Knepp, Proc. IEEE 71, 722 (1983).
        """
        
        npix = wave.shape[0]
        pixelscale = wave.pixelscale.to(u.m/u.pixel) * u.pix
        dq = 2.0*np.pi/npix/pixelscale
        
        # create complex random numbers with required symmetry
        a = self.rand_turbulent(npix)
        
        # get phase spectrum
        phi = self.power_spectrum(wave=wave, kind=self.kind)
        
        # calculate OPD
        # Note: Factor dq consequence of delta function having a unit
        opd_FFT = dq*a*np.sqrt(2.0*np.pi*self.dz*phi)
        opd = npix**2*np.fft.ifft2(opd_FFT)
        
        self.opd = opd.real.value
        
        return self.opd
    
    @utils.quantity_input(wavelength=u.meter)
    def get_Cn2(self, wavelength):
        """ Returns the index-of-refraction structure constant (m^-2/3).
        
        Parameters
        -----------------
        wavelength : float
            The wavelength (m).
        
        References
        -------------------
        B. J. Herman and L. A. Strugala, in Proc. SPIE,
        edited by P. B. Ulrich and L. E. Wilson
        (International Society for Optics and Photonics, 1990), pp. 183–192.
        """
        
        if all(item is not None for item in [self.r0, self.dz]):
            r0 = self.r0.to(u.m)
            wavelength2 = wavelength.to(u.m)**2
            return wavelength2/self.dz * (r0/0.185)**(-5.0/3.0)
        elif self.Cn2 is not None:
            return self.Cn2.to(u.m**(-2/3))
    
    def rand_symmetrized(self, npix, sign):
        """ Returns a real-valued random number array of shape (npix, npix)
        with the symmetry required for a turbulent phase screen.
        
        Parameters
        -----------------
        npix : int
            Number of pixels.
        
        sign : int
            Sign of mirror symmetry. Must be either +1 or -1.
        
        References
        -------------------
        Eq. (65) in J. A. Fleck Jr, J. R. Morris, and M. D. Feit,
        Appl. Phys. 10, 129 (1976).
        """
        
        if np.abs(sign) != 1:
            raise ValueError("sign must be either +1 or -1")
        
        sign = float(sign)
        
        # create Gaussian, zero-mean, unit variance random numbers
        random_numbers = np.random.RandomState()
        random_numbers.seed(self.seed)
        a = random_numbers.normal(size=(npix, npix))
        
        # apply required symmetry
        a[0, int(npix/2)+1:npix] = sign*a[0, 1:int(npix/2)][::-1]
        a[int(npix/2)+1:npix, 0] = sign*a[1:int(npix/2), 0][::-1]
        a[int(npix/2)+1:npix, int(npix/2)+1:npix] = sign*np.rot90(a[1:int(npix/2), 1:int(npix/2)], 2)
        a[int(npix/2)+1:npix, 1:int(npix/2)] = sign*np.rot90(a[1:int(npix/2), int(npix/2)+1:npix], 2)
        
        # remove any overall phase resulting from the zero-frequency component
        a[0, 0] = 0.0
        
        return a
    
    def rand_turbulent(self, npix):
        """ Returns a complex-valued random number array of shape (npix, npix)
        with the symmetry required for a turbulent phase screen.
        
        Parameters
        -----------------
        npix : int
            Number of pixels.
        
        References
        -------------------
        Eq. (63) in J. A. Fleck Jr, J. R. Morris, and M. D. Feit,
        Appl. Phys. 10, 129 (1976).
        """
        
        # create real-valued random numbers with required symmetry
        a = self.rand_symmetrized(npix, 1)
        b = self.rand_symmetrized(npix, -1)
        
        # create complex-valued random number with required variance
        c = (a + 1j*b)/np.sqrt(2.0)
        
        return c
    
    def power_spectrum(self, wave, kind='Kolmogorov'):
        """ Returns the spatial power spectrum.
        
        Parameters
        -----------------
        wave : wavefront object
            Wavefront to calculate the power spectrum for.
        
        kind : string
            The type of the power spectrum, must be one of 'Kolmogorov',
            'Tatarski', 'von Karman', 'Hill'.
        
        References
        -------------------
        G. Gbur, J. Opt. Soc. Am. A 31, 2038 (2014).
        
        R. Frehlich, Appl. Opt. 39, 393 (2000).
        """
        
        if not any(kind==item for item in ['Kolmogorov', 'Tatarski', 'von Karman', 'Hill']):
            raise ValueError('Kind of power spectrum not correctly defined.')
        
        Cn2 = self.get_Cn2(wave.wavelength)
        coordinates = wave.coordinates()
        npix = coordinates[0].shape[0]
        pixelscale = wave.pixelscale.to(u.m/u.pixel) * u.pix
        
        q = np.fft.fftfreq(npix, d=pixelscale)*2.0*np.pi
        
        qx, qy = np.meshgrid(q, q)
        
        q2 = (qx**2 + qy**2)
        if kind=='von Karman':
            if self.outer_scale is not None:
                q2 += 1.0/self.outer_scale.to(u.m)**2
            else:
                raise ValueError('If von Karman kind of turbulent phase \
                                 screen is chosen, the outer scale L_0 \
                                 must be provided.')
        q2[0, 0] = np.inf # this is to avoid a possible error message in the next line
        
        phi = 0.0330054*Cn2*q2**(-11.0/6.0)
        
        if kind=='Tatarski' or kind=='von Karman' or kind=='Hill':
            if self.inner_scale is not None:
                k2 = (qx**2 + qy**2)
                if kind=='Tatarski' or kind=='von Karman':
                    m = (5.92/self.inner_scale.to(u.m))**2
                    phi *= np.exp(-k2/m)
                elif kind=='Hill':
                    m = np.sqrt(k2)*self.inner_scale.to(u.m)
                    phi *= (1.0 + 0.70937*m + 2.8235*m**2
                            - 0.28086*m**3 + 0.08277*m**4) * np.exp(-1.109*m)
            else:
                raise ValueError('If von Karman, Hill, or Tatarski kind \
                                 of turbulent phase screen is chosen, the \
                                 inner scale l_0 must be provided.')
        
        return phi

