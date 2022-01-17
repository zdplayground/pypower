import os
import tempfile

import numpy as np
from matplotlib import pyplot as plt
from mpi4py import MPI

from cosmoprimo.fiducial import DESI
from mockfactory import LagrangianLinearMock, Catalog
from mockfactory.make_survey import RandomBoxCatalog

from pypower import MeshFFTPower, CatalogFFTPower, CatalogMesh, PowerSpectrumStatistic, mpi, utils, setup_logging
from pypower.fft_power import normalization, normalization_from_nbar, find_unique_edges


base_dir = 'catalog'
data_fn = os.path.join(base_dir, 'lognormal_data.fits')
randoms_fn = os.path.join(base_dir, 'lognormal_randoms.fits')


def save_lognormal():
    z = 1.
    boxsize = 600.
    boxcenter = 0.
    los = 'x'
    nbar = 1e-3
    bias = 2.0
    nmesh = 256
    seed = 42
    power = DESI().get_fourier().pk_interpolator().to_1d(z=z)
    f = 0.8
    mock = LagrangianLinearMock(power, nmesh=nmesh, boxsize=boxsize, boxcenter=boxcenter, seed=seed, unitary_amplitude=True)
    mock.set_real_delta_field(bias=bias-1.)
    mock.set_analytic_selection_function(nbar=nbar)
    mock.poisson_sample(seed=seed, resampler='cic', compensate=True)
    mock.set_rsd(f=f, los=los)
    #mock.set_rsd(f=f)
    data = mock.to_catalog()
    offset = mock.boxcenter - mock.boxsize / 2.
    data['Position'] = (data['Position'] - offset) % mock.boxsize + offset
    randoms = RandomBoxCatalog(nbar=4.*nbar, boxsize=boxsize, boxcenter=boxcenter, seed=44)

    for catalog in [data, randoms]:
        catalog['NZ'] = nbar*catalog.ones()
        catalog['WEIGHT_FKP'] = np.ones(catalog.size, dtype='f8')

    data.save_fits(data_fn)
    randoms.save_fits(randoms_fn)


def test_power_statistic():
    edges = np.linspace(0., 0.2, 11)
    modes = (edges[:-1] + edges[1:])/2.
    nmodes = np.ones_like(modes, dtype='i8')
    ells = (0, 2, 4)
    power = [np.ones_like(modes)]*len(ells)
    power = PowerSpectrumStatistic(edges, modes, power, nmodes, ells, statistic='multipole')
    power.rebin(factor=2)
    assert np.allclose(power.k, (modes[::2] + modes[1::2])/2.)
    assert np.allclose(power.kedges, np.linspace(0., 0.2, 6))
    assert power.shape == (modes.size//2,)
    with tempfile.TemporaryDirectory() as tmp_dir:
        fn = os.path.join(tmp_dir, 'tmp.npy')
        power.save(fn)
        test = PowerSpectrumStatistic.load(fn)
        assert np.all(test.power == power.power)
    power2 = power.copy()
    power2.modes[0] = 1.
    assert np.all(power.modes[0] == test.modes[0])

    edges = (edges, np.linspace(0., 1., 21))
    nmodes = np.ones(tuple(len(e)-1 for e in edges), dtype='i8')
    modes = [nmodes.astype('f8'), nmodes.astype('f8')]
    power = nmodes.astype('f8')
    power = PowerSpectrumStatistic(edges, modes, power, nmodes, statistic='wedge')
    power.rebin(factor=(2, 2))
    assert power.modes[0].shape == (5, 10)


def test_find_edges():
    x = np.meshgrid(np.arange(10.), np.arange(10.), indexing='ij')
    x0 = np.ones(len(x), dtype='f8')
    edges = find_unique_edges(x, x0, xmin=0., xmax=np.inf, mpicomm=mpi.COMM_WORLD)


def test_mesh_field_power():

    z = 1.
    bias, nbar, nmesh, boxsize, boxcenter = 2.0, 1e-3, 128, 1000., 500.
    power = DESI().get_fourier().pk_interpolator().to_1d(z=z)
    mock = LagrangianLinearMock(power, nmesh=nmesh, boxsize=boxsize, boxcenter=boxcenter, seed=42, unitary_amplitude=False)
    # This is Lagrangian bias, Eulerian bias - 1
    mock.set_real_delta_field(bias=bias-1)
    mesh_real = mock.mesh_delta_r + 1.

    kedges = np.linspace(0., 0.4, 11)
    muedges = np.linspace(-1., 1., 5)
    dk = kedges[1] - kedges[0]
    ells = (0, 1, 2, 3, 4)

    def get_ref_power(mesh, los):
        from nbodykit.lab import FFTPower
        return FFTPower(mesh, mode='2d', poles=ells, Nmu=len(muedges) - 1, los=los, dk=dk, kmin=kedges[0], kmax=kedges[-1])

    def get_mesh_power(mesh, los, edges=(kedges, muedges)):
        return MeshFFTPower(mesh, ells=ells, los=los, edges=edges)

    def check_wedges(power, ref_power):
        for imu, mu in enumerate(power.muavg):
            if hasattr(ref_power, 'k'):
                k, mu, modes, pk = ref_power.k[:,imu], ref_power.mu[:,imu], ref_power.nmodes[:,imu], ref_power.power[:,imu] + ref_power.shotnoise
            else:
                k, mu, modes, pk = ref_power['k'][:,imu], ref_power['mu'][:,imu], ref_power['modes'][:,imu], ref_power['power'][:,imu].conj()
            print(power.power[:,imu] + power.shotnoise)
            print(pk)
            print(power.nmodes[:,imu], modes)
            assert np.allclose(power.nmodes[:,imu], modes, atol=1e-6, rtol=3e-3, equal_nan=True)
            assert np.allclose(power.k[:,imu], k, atol=1e-6, rtol=3e-3, equal_nan=True)
            assert np.allclose(power.mu[:,imu], mu, atol=1e-6, rtol=3e-3, equal_nan=True)
            #assert np.allclose(power.power[:,imu] + power.shotnoise, pk, atol=1e-6, rtol=3e-3, equal_nan=True)

    def check_poles(power, ref_power):
        for ell in power.ells:
            if hasattr(ref_power, 'k'):
                k, modes, pk = ref_power.k, ref_power.nmodes, ref_power(ell=ell) + ref_power.shotnoise
            else:
                k, modes, pk = ref_power['k'], ref_power['modes'], ref_power['power_{}'.format(ell)].conj()
            assert np.allclose(power.nmodes, modes, atol=1e-6, rtol=5e-3)
            assert np.allclose(power.k, k, atol=1e-6, rtol=5e-3)
            assert np.allclose(power(ell=ell) + (ell == 0)*power.shotnoise, pk, atol=1e-2, rtol=5e-3)

    from pypower import ParticleMesh
    pm = ParticleMesh(BoxSize=mesh_real.pm.BoxSize, Nmesh=mesh_real.pm.Nmesh, dtype='c16', comm=mesh_real.pm.comm)
    mesh_complex = pm.create(type='real')
    mesh_complex[...] = mesh_real[...]
    #print(kedges <= np.pi*nmesh/boxsize)

    for los in [(1,0,0), (0,1,0), (0,0,1)][2:]:
        #ref_power = get_ref_power(mesh_complex if los == (0,0,1) else mesh_real, los)
        ref_power = get_ref_power(mesh_complex, los)
        ref_kedges = ref_power.power.edges['k']
        power = get_mesh_power(mesh_real, los, edges=(ref_kedges, muedges))
        #check_wedges(power.wedges, ref_power.power)
        #check_poles(power.poles, ref_power.poles)

        c_power = get_mesh_power(mesh_complex, los, edges=(ref_kedges, muedges))
        #check_wedges(power.wedges, c_power.wedges)
        check_poles(power.poles, c_power.poles)


def test_mesh_power():
    boxsize = 600.
    boxcenter = 0.
    nmesh = 128
    kedges = np.linspace(0., 0.4, 11)
    muedges = np.linspace(-1., 1., 5)
    dk = kedges[1] - kedges[0]
    ells = (0, 1, 2, 4)
    resampler = 'cic'
    interlacing = 2
    dtype = 'f8'
    data = Catalog.load_fits(data_fn)

    def get_ref_power(data, los, dtype='c16'):
        los_array = [1. if ax == los else 0. for ax in 'xyz']
        from nbodykit.lab import FFTPower
        mesh = data.to_nbodykit().to_mesh(position='Position', BoxSize=boxsize, Nmesh=nmesh, resampler=resampler, interlaced=bool(interlacing), compensated=True, dtype=dtype)
        return FFTPower(mesh, mode='2d', poles=ells, Nmu=len(muedges) - 1, los=los_array, dk=dk, kmin=kedges[0])

    def get_mesh_power(data, los, edges=(kedges, muedges), dtype=dtype):
        mesh = CatalogMesh(data_positions=data['Position'], boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos', dtype=dtype)
        return MeshFFTPower(mesh, ells=ells, los=los, edges=edges)

    def get_mesh_power_compensation(data, los):
        mesh = CatalogMesh(data_positions=data['Position'], boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos', dtype=dtype).to_mesh()
        return MeshFFTPower(mesh, ells=ells, los=los, edges=(kedges, muedges), compensations=resampler)

    def get_mesh_power_cross(data, los):
        mesh1 = CatalogMesh(data_positions=data['Position'].T, boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='xyz')
        mesh2 = CatalogMesh(data_positions=data['Position'], boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos')
        return MeshFFTPower(mesh1, mesh2=mesh2, ells=ells, los=los, edges=kedges)

    def check_wedges(power, ref_power):
        for imu, mu in enumerate(power.muavg):
            assert np.allclose(power(mu=mu) + power.shotnoise, ref_power['power'][:,imu].conj(), atol=1e-6, rtol=3e-3, equal_nan=True)
            assert np.allclose(power.k[:,imu], ref_power['k'][:,imu], atol=1e-6, rtol=3e-3, equal_nan=True)
            assert np.allclose(power.nmodes[:,imu], ref_power['modes'][:,imu], atol=1e-6, rtol=3e-3, equal_nan=True)

    def check_poles(power, ref_power):
        for ell in power.ells:
            #assert np.allclose(power(ell=ell).real + (ell == 0)*power.shotnoise, ref_power.poles['power_{}'.format(ell)].real, atol=1e-6, rtol=3e-3)
            # Exact if offset = 0. in to_mesh()
            assert np.allclose(power(ell=ell) + (ell == 0)*power.shotnoise, ref_power['power_{}'.format(ell)].conj(), atol=1e-2, rtol=5e-3)
            assert np.allclose(power.k, ref_power['k'], atol=1e-6, rtol=5e-3)
            assert np.allclose(power.nmodes, ref_power['modes'], atol=1e-6, rtol=5e-3)

    for los in ['x', 'z']:

        ref_power = get_ref_power(data, los=los)
        ref_kedges = ref_power.power.edges['k']

        list_options = []
        list_options.append({'los':los, 'edges':(ref_kedges, muedges)})
        list_options.append({'los':[1. if ax == los else 0. for ax in 'xyz'], 'edges':(ref_kedges, muedges)})
        list_options.append({'los':los, 'edges':({'min':ref_kedges[0], 'max':ref_kedges[-1], 'step':ref_kedges[1] - ref_kedges[0]}, muedges)})
        list_options.append({'los':los, 'edges':(ref_kedges, muedges), 'dtype':'f4'})
        list_options.append({'los':los, 'edges':(ref_kedges, muedges[:-1]), 'dtype':'f4'})
        list_options.append({'los':los, 'edges':(ref_kedges, muedges[:-1]), 'dtype':'c8'})
        for options in list_options:
            power = get_mesh_power(data, **options)

            with tempfile.TemporaryDirectory() as tmp_dir:
                fn = power.mpicomm.bcast(os.path.join(tmp_dir, 'tmp.npy'), root=0)
                power.save(fn)
                power = MeshFFTPower.load(fn)

            check_wedges(power.wedges, ref_power.power)

            if power.wedges.edges[1][-1] == 1.:
                check_poles(power.poles, ref_power.poles)

    power = get_mesh_power(data, los='x').poles
    power_compensation = get_mesh_power_compensation(data, los='x').poles
    for ill, ell in enumerate(power.ells):
        assert np.allclose(power_compensation.power_nonorm[ill]/power_compensation.wnorm, power.power_nonorm[ill]/power.wnorm)

    power_cross = get_mesh_power_cross(data, los='x').poles
    for ell in ells:
        assert np.allclose(power_cross(ell=ell) - (ell == 0)*power.shotnoise, power(ell=ell))

    randoms = Catalog.load_fits(randoms_fn)

    def get_ref_power(data, randoms, los, dtype='c16'):
        los_array = [1. if ax == los else 0. for ax in 'xyz']
        from nbodykit.lab import FFTPower
        mesh_data = data.to_nbodykit().to_mesh(position='Position', BoxSize=boxsize, Nmesh=nmesh, resampler=resampler, interlaced=bool(interlacing), compensated=True, dtype=dtype)
        mesh_randoms = randoms.to_nbodykit().to_mesh(position='Position', BoxSize=boxsize, Nmesh=nmesh, resampler=resampler, interlaced=bool(interlacing), compensated=True, dtype=dtype)
        mesh = mesh_data.compute() - mesh_randoms.compute()
        return FFTPower(mesh, mode='2d', poles=ells, Nmu=len(muedges) - 1, los=los_array, dk=dk, kmin=kedges[0], kmax=kedges[-1]+1e-9)

    def get_power(data, randoms, los, dtype=dtype):
        mesh = CatalogMesh(data_positions=data['Position'], randoms_positions=randoms['Position'], boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos', dtype=dtype)
        wnorm = normalization(mesh, uniform=True)
        return MeshFFTPower(mesh, ells=ells, los=los, edges=(kedges, muedges), wnorm=wnorm)

    ref_power = get_ref_power(data, randoms, los='x')
    power = get_power(data, randoms, los='x')
    check_wedges(power.wedges, ref_power.power)
    check_poles(power.poles, ref_power.poles)


def test_normalization():
    boxsize = 1000.
    nmesh = 128
    resampler = 'tsc'
    interlacing = False
    boxcenter = np.array([3000.,0.,0.])[None,:]
    los = None
    dtype = 'f8'
    data = Catalog.load_fits(data_fn)
    randoms = Catalog.load_fits(randoms_fn)
    for catalog in [data, randoms]:
        catalog['Position'] += boxcenter
        catalog['Weight'] = catalog.ones()
    mesh = CatalogMesh(data_positions=data['Position'], data_weights=data['Weight'], randoms_positions=randoms['Position'], randoms_weights=randoms['Weight'],
                       boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos', dtype=dtype)
    old = normalization_from_nbar(randoms['NZ'], randoms['Weight'], data_weights=data['Weight'], mpicomm=mesh.mpicomm)
    new = normalization(mesh)
    assert np.allclose(new, old, atol=0, rtol=1e-1)


def test_catalog_power():
    boxsize = 1000.
    nmesh = 128
    kedges = np.linspace(0., 0.3, 6)
    dk = kedges[1] - kedges[0]
    ells = (0, 1, 2, 3, 4)
    resampler = 'tsc'
    interlacing = 2
    boxcenter = np.array([3000.,0.,0.])[None,:]
    los = None
    dtype = 'f8'
    data = Catalog.load_fits(data_fn)
    randoms = Catalog.load_fits(randoms_fn)
    for catalog in [data, randoms]:
        catalog['Position'] += boxcenter
        catalog['Weight'] = catalog.ones()

    def get_ref_power(data, randoms, dtype='c16'):
        from nbodykit.lab import FKPCatalog, ConvolvedFFTPower
        fkp = FKPCatalog(data.to_nbodykit(), randoms.to_nbodykit(), nbar='NZ')
        mesh = fkp.to_mesh(position='Position', comp_weight='Weight', nbar='NZ', BoxSize=boxsize, Nmesh=nmesh, resampler=resampler, interlaced=bool(interlacing), compensated=True, dtype=dtype)
        return ConvolvedFFTPower(mesh, poles=ells, dk=dk, kmin=kedges[0], kmax=kedges[-1]+1e-9)

    def get_catalog_power(data, randoms, position_type='pos', edges=kedges, dtype=dtype):
        data_positions, randoms_positions = data['Position'], randoms['Position']
        if position_type == 'xyz':
            data_positions, randoms_positions = data['Position'].T, randoms['Position'].T
        elif position_type == 'rdd':
            data_positions, randoms_positions = utils.cartesian_to_sky(data['Position'].T), utils.cartesian_to_sky(randoms['Position'].T)
        return CatalogFFTPower(data_positions1=data_positions, data_weights1=data['Weight'], randoms_positions1=randoms_positions, randoms_weights1=randoms['Weight'],
                               boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, ells=ells, los=los, edges=edges, position_type=position_type, dtype=dtype)

    def get_catalog_mesh_power(data, randoms, dtype=dtype):
        mesh = CatalogMesh(data_positions=data['Position'], data_weights=data['Weight'], randoms_positions=randoms['Position'], randoms_weights=randoms['Weight'],
                           boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, position_type='pos', dtype=dtype)
        return MeshFFTPower(mesh, ells=ells, los=los, edges=kedges)

    def check_poles(power, ref_power):
        norm = power.wnorm
        ref_norm = ref_power.attrs['randoms.norm']
        for ell in power.ells:
            # precision is 1e-7 if offset = self.boxcenter - self.boxsize/2. + 0.5*self.boxsize
            ref = ref_power.poles['power_{}'.format(ell)]
            if power.attrs['los_type'] == 'endpoint': ref = ref.conj()
            assert np.allclose((power(ell=ell) + (ell == 0)*power.shotnoise)*norm/ref_norm, ref, atol=1e-6, rtol=5e-2)
            assert np.allclose(power.k, ref_power.poles['k'], atol=1e-6, rtol=5e-3)
            assert np.allclose(power.nmodes, ref_power.poles['modes'], atol=1e-6, rtol=5e-3)

    ref_power = get_ref_power(data, randoms)
    f_power = get_catalog_power(data, randoms, dtype='f8')
    c_power = get_catalog_power(data, randoms, dtype='c16')
    ref_kedges = ref_power.poles.edges['k']

    list_options = []
    list_options.append({'position_type':'pos'})
    #list_options.append({'position_type':'xyz'})
    #list_options.append({'position_type':'rdd'})
    #list_options.append({'edges':{'min':ref_kedges[0],'max':ref_kedges[-1],'step':ref_kedges[1] - ref_kedges[0]}})

    for options in list_options:
        power = get_catalog_power(data, randoms, **options)

        with tempfile.TemporaryDirectory() as tmp_dir:
            fn = power.mpicomm.bcast(os.path.join(tmp_dir, 'tmp.npy'), root=0)
            power.save(fn)
            power = CatalogFFTPower.load(fn)

        check_poles(power.poles, ref_power)
        for ell in ells:
            atol = 2e-1 if ell % 2 == 0 else 1e-5
            assert np.allclose(power.poles(ell=ell).imag, c_power.poles(ell=ell).imag, atol=atol, rtol=1e-3)
            atol = 2e-1 if ell % 2 else 1e-5
            assert np.allclose(power.poles(ell=ell).real, c_power.poles(ell=ell).real, atol=atol, rtol=1e-3)

    power_mesh = get_catalog_mesh_power(data, randoms)
    for ell in ells:
        assert np.allclose(power_mesh.poles(ell=ell), f_power.poles(ell=ell))

    def get_catalog_power_cross(data, randoms):
        return CatalogFFTPower(data_positions1=data['Position'].T, data_weights1=data['Weight'], randoms_positions1=randoms['Position'].T, randoms_weights1=randoms['Weight'],
                               data_positions2=data['Position'].T, data_weights2=data['Weight'], randoms_positions2=randoms['Position'].T, randoms_weights2=randoms['Weight'],
                               boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, ells=ells, los=los, edges=kedges, position_type='xyz')

    power_cross = get_catalog_power_cross(data, randoms)
    for ell in ells:
        assert np.allclose(power_cross.poles(ell=ell) - (ell == 0)*f_power.shotnoise, f_power.poles(ell=ell))


def test_mpi():
    boxsize = 1000.
    nmesh = 128
    kedges = np.linspace(0., 0.1, 6)
    dk = kedges[1] - kedges[0]
    ells = (0,)
    resampler = 'tsc'
    interlacing = 2
    boxcenter = np.array([3000.,0.,0.])[None,:]
    dtype = 'f8'
    cdtype = 'c16'
    los = None
    data = Catalog.load_fits(data_fn)
    randoms = Catalog.load_fits(randoms_fn)
    for catalog in [data, randoms]:
        catalog['Position'] += boxcenter
        catalog['Weight'] = catalog.ones()

    def run(mpiroot=None, mpicomm=mpi.COMM_WORLD):
        return CatalogFFTPower(data_positions1=data['Position'], data_weights1=data['Weight'], randoms_positions1=randoms['Position'], randoms_weights1=randoms['Weight'],
                               boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, ells=ells, los=los, edges=kedges, position_type='pos',
                               dtype=dtype, mpiroot=mpiroot, mpicomm=mpicomm).poles

    ref_power = run(mpiroot=None, mpicomm=data.mpicomm)
    for catalog in [data, randoms]:
        catalog['Position'] = mpi.gather_array(catalog['Position'], root=0, mpicomm=catalog.mpicomm)
        catalog['Weight'] = mpi.gather_array(catalog['Weight'], root=0, mpicomm=catalog.mpicomm)

    power = run(mpiroot=0, mpicomm=data.mpicomm)
    for ell in power.ells:
        assert np.allclose(power(ell=ell), ref_power(ell=ell))

    if data.mpicomm.rank == 0:
        power = run(mpiroot=0, mpicomm=MPI.COMM_SELF)
        for ell in power.ells:
            assert np.allclose(power(ell=ell), ref_power(ell=ell))


def test_interlacing():

    from matplotlib import pyplot as plt
    boxsize = 1000.
    nmesh = 128
    kedges = {'min':0., 'step':0.005}
    ells = (0,)
    resampler = 'ngp'
    boxcenter = np.array([3000.,0.,0.])[None,:]

    data = Catalog.load_fits(data_fn)
    randoms = Catalog.load_fits(randoms_fn)
    for catalog in [data, randoms]:
        catalog['Position'] += boxcenter
        catalog['Weight'] = catalog.ones()

    def run(interlacing=2):
        return CatalogFFTPower(data_positions1=data['Position'], data_weights1=data['Weight'], randoms_positions1=randoms['Position'], randoms_weights1=randoms['Weight'],
                               boxsize=boxsize, nmesh=nmesh, resampler=resampler, interlacing=interlacing, ells=ells, los='firstpoint', edges=kedges, position_type='pos').poles

    for interlacing, linestyle in zip([False, 2, 3, 4], ['-', '--', ':', '-.']):
        power = run(interlacing=interlacing)
        for ill, ell in enumerate(power.ells):
            plt.plot(power.k, power.k * power(ell=ell).real, color='C{:d}'.format(ill), linestyle=linestyle, label='interlacing = {}'.format(interlacing))
    plt.legend()
    plt.show()


if __name__ == '__main__':

    setup_logging()
    #save_lognormal()
    #test_mesh_power()
    test_mesh_field_power()
    #test_interlacing()
    test_power_statistic()
    test_find_edges()
    test_mesh_power()
    test_catalog_power()
    test_normalization()
    test_mpi()
