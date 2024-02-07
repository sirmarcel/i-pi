"""Contains the classes that deal with the different dynamics required in
different types of ensembles.

Holds the algorithms required for normal mode propagators, and the objects to
do the constant temperature and pressure algorithms. Also calculates the
appropriate conserved energy quantity for the ensemble of choice.
"""

# This file is part of i-PI.
# i-PI Copyright (C) 2014-2015 i-PI developers
# See the "licenses" directory for full license information.

import numpy as np

from ipi.engine.motion import Motion
from ipi.utils.depend import *
from ipi.engine.thermostats import Thermostat
from ipi.engine.barostats import Barostat
from ipi.utils.softexit import softexit
from ipi.utils.messages import warning, verbosity
from ipi.engine.eda import EDA
from ipi.utils.units import Constants


class Dynamics(Motion):
    """self (path integral) molecular dynamics class.

    Gives the standard methods and attributes needed in all the
    dynamics classes.

    Attributes:
        beads: A beads object giving the atoms positions.
        cell: A cell object giving the system box.
        forces: A forces object giving the virial and the forces acting on
            each bead.
        prng: A random number generator object.
        nm: An object which does the normal modes transformation.

    Depend objects:
        econs: The conserved energy quantity appropriate to the given
            ensemble. Depends on the various energy terms which make it up,
            which are different depending on the ensemble.he
        temp: The system temperature.
        dt: The timestep for the algorithms.
        ntemp: The simulation temperature. Will be nbeads times higher than
            the system temperature as PIMD calculations are done at this
            effective classical temperature.
    """

    def __init__(
        self,
        timestep,
        mode="nve",
        splitting="obabo",
        thermostat=None,
        barostat=None,
        fixcom=False,
        fixatoms=None,
        nmts=None,
        efield=None,
        bec=None,
    ):
        """Initialises a "dynamics" motion object.

        Args:
            dt: The timestep of the simulation algorithms.
            fixcom: An optional boolean which decides whether the centre of mass
                motion will be constrained or not. Defaults to False.
        """

        super(Dynamics, self).__init__(fixcom=fixcom, fixatoms=fixatoms)

        # initialize time step. this is the master time step that covers a full time step
        self._dt = depend_value(name="dt", value=timestep)

        if thermostat is None:
            self.thermostat = Thermostat()
        else:
            if (
                thermostat.__class__.__name__ is ("ThermoPILE_G" or "ThermoNMGLEG ")
            ) and (len(fixatoms) > 0):
                softexit.trigger(
                    status="bad",
                    message="!! Sorry, fixed atoms and global thermostat on the centroid not supported. Use a local thermostat. !!",
                )
            self.thermostat = thermostat

        if nmts is None or len(nmts) == 0:
            self._nmts = depend_array(name="nmts", value=np.asarray([1], int))
        else:
            self._nmts = depend_array(name="nmts", value=np.asarray(nmts, int))

        if barostat is None:
            self.barostat = Barostat()
        else:
            self.barostat = barostat
        self.enstype = mode
        if self.enstype == "nve":
            self.integrator = NVEIntegrator()
        elif self.enstype == "nvt":
            self.integrator = NVTIntegrator()
        elif self.enstype == "nvt-cc":
            self.integrator = NVTCCIntegrator()
        elif self.enstype == "npt":
            self.integrator = NPTIntegrator()
        elif self.enstype == "nst":
            self.integrator = NSTIntegrator()
        elif self.enstype == "sc":
            self.integrator = SCIntegrator()
        elif self.enstype == "scnpt":
            self.integrator = SCNPTIntegrator()
        elif self.enstype == "eda-nve":
            # NVE integrator with an external time-dependent driving (electric field)
            self.integrator = EDANVEIntegrator()
        else:
            self.integrator = DummyIntegrator()

        # splitting mode for the integrators
        self._splitting = depend_value(name="splitting", value=splitting)

        # constraints
        self.fixcom = fixcom
        if fixatoms is None:
            self.fixatoms = np.zeros(0, int)
        else:
            self.fixatoms = fixatoms

        self.eda_on = False # whether the dynamics is driven or not
        if self.enstype in EDA.integrators:
            # if the dynamics is driven, allocate necessary objects  
            self.efield = efield
            self.bec = bec
            self.eda = EDA(self.efield, self.bec)
            self.eda_on = True
        else:
            # otherwise, keep the class clean and avoid allocating useless things
            pass

    def get_fixdof(self):
        """Calculate the number of fixed degrees of freedom, required for
        temperature and pressure calculations.
        """

        fixdof = len(self.fixatoms) * 3 * self.beads.nbeads
        if self.fixcom:
            fixdof += 3
        return fixdof

    def bind(self, ens, beads, nm, cell, bforce, prng, omaker):
        """Binds ensemble beads, cell, bforce, and prng to the dynamics.

        This takes a beads object, a cell object, a forcefield object and a
        random number generator object and makes them members of the ensemble.
        It also then creates the objects that will hold the data needed in the
        ensemble algorithms and the dependency network. Note that the conserved
        quantity is defined in the init, but as each ensemble has a different
        conserved quantity the dependencies are defined in bind.

        Args:
            beads: The beads object from which the bead positions are taken.
            nm: A normal modes object used to do the normal modes transformation.
            cell: The cell object from which the system box is taken.
            bforce: The forcefield object from which the force and virial are
                taken.
            prng: The random number generator object which controls random number
                generation.
        """

        super(Dynamics, self).bind(ens, beads, nm, cell, bforce, prng, omaker)

        # Checks if the number of mts levels is equal to the dimensionality of the mts weights.
        if len(self.nmts) != self.forces.nmtslevels:
            raise ValueError(
                "The number of mts levels for the integrator does not agree with the mts_weights of the force components."
            )

        # n times the temperature (for path integral partition function)
        self._ntemp = depend_value(
            name="ntemp", func=self.get_ntemp, dependencies=[self.ensemble._temp]
        )

        # fixed degrees of freedom count
        fixdof = self.get_fixdof()

        # first makes sure that the thermostat has the correct temperature and timestep, then proceeds with binding it.
        dpipe(self._ntemp, self.thermostat._temp)

        # depending on the kind, the thermostat might work in the normal mode or the bead representation.
        self.thermostat.bind(beads=self.beads, nm=self.nm, prng=prng, fixdof=fixdof)

        # first makes sure that the barostat has the correct stress and timestep, then proceeds with binding it.
        dpipe(self._ntemp, self.barostat._temp)
        dpipe(self.ensemble._pext, self.barostat._pext)
        dpipe(self.ensemble._stressext, self.barostat._stressext)
        self.barostat.bind(
            beads,
            nm,
            cell,
            bforce,
            bias=self.ensemble.bias,
            prng=prng,
            fixdof=fixdof,
            nmts=len(self.nmts),
        )

        # now that the timesteps are decided, we proceed to bind the integrator.
        self.integrator.bind(self)

        self.ensemble.add_econs(self.thermostat._ethermo)
        self.ensemble.add_econs(self.barostat._ebaro)

        # adds the potential, kinetic energy and the cell Jacobian to the ensemble
        self.ensemble.add_xlpot(self.barostat._pot)
        self.ensemble.add_xlpot(self.barostat._cell_jacobian)
        self.ensemble.add_xlkin(self.barostat._kin)

        # self.eda has not been allocated if 'self.eda_on' == False
        # Attention: call 'self.eda.bind' before 'self.integrator.bind'
        if self.eda_on:
            self.eda.bind(self.ensemble, self.enstype)

        # now that the timesteps are decided, we proceed to bind the integrator.
        self.integrator.bind(self)

        # applies constraints immediately after initialization.
        self.integrator.pconstraints()

        # TODO THOROUGH CLEAN-UP AND CHECK
        if (
            self.enstype == "nvt"
            or self.enstype == "nvt-cc"
            or self.enstype == "npt"
            or self.enstype == "nst"
        ):
            if self.ensemble.temp < 0:
                raise ValueError(
                    "Negative or unspecified temperature for a constant-T integrator"
                )
            if self.enstype == "npt":
                if type(self.barostat) is Barostat:
                    raise ValueError(
                        "The barostat and its mode have to be specified for constant-p integrators"
                    )
                if np.allclose(self.ensemble.pext, -12345):
                    raise ValueError("Unspecified pressure for a constant-p integrator")
            elif self.enstype == "nst":
                if np.allclose(self.ensemble.stressext.diagonal(), -12345):
                    raise ValueError("Unspecified stress for a constant-s integrator")

    def get_ntemp(self):
        """Returns the PI simulation temperature (P times the physical T)."""

        return self.ensemble.temp * self.beads.nbeads

    def step(self, step=None):
        """Advances the dynamics by one time step"""

        self.integrator.step(step)
        self.ensemble.time += self.dt  # increments internal time

        if self.eda_on:
            # Check that these variable are the same.
            # If they are not the same, then there is a bug in the code
            dt = abs(self.ensemble.time - self.integrator.mts_time)
            if dt > 1e-12:
                raise ValueError(
                    "The time at which the Electric Field is evaluated is not properly updated!"
                )


dproperties(Dynamics, ["dt", "nmts", "splitting", "ntemp"])


class DummyIntegrator:
    """No-op integrator for (PI)MD"""

    def __init__(self):
        pass

    def get_qdt(self):
        return self.dt * 0.5 / self.inmts

    def get_pdt(self):
        dtl = 1.0 / self.nmts
        for i in range(1, len(dtl)):
            dtl[i] *= dtl[i - 1]
        dtl *= self.dt * 0.5
        return dtl

    def get_tdt(self):
        if self.splitting == "obabo":
            return self.dt * 0.5
        elif self.splitting == "baoab":
            return self.dt
        else:
            raise ValueError(
                "Invalid splitting requested. Only OBABO and BAOAB are supported."
            )

    def bind(self, motion):
        """Reference all the variables for simpler access."""

        self.beads = motion.beads
        self.bias = motion.ensemble.bias
        self.ensemble = motion.ensemble
        self.forces = motion.forces
        self.prng = motion.prng
        self.nm = motion.nm
        self.thermostat = motion.thermostat
        self.barostat = motion.barostat
        self.fixcom = motion.fixcom
        self.fixatoms = motion.fixatoms
        self.enstype = motion.enstype
        if motion.enstype in EDA.integrators:
            self.eda = motion.eda

        # no need to dpipe these are really just references
        self._splitting = motion._splitting
        self._dt = motion._dt
        self._nmts = motion._nmts

        # total number of iteration in the inner-most MTS loop
        self._inmts = depend_value(name="inmts", func=lambda: np.prod(self.nmts))
        self._nmtslevels = depend_value(name="nmtslevels", func=lambda: len(self.nmts))
        # these are the time steps to be used for the different parts of the integrator
        self._qdt = depend_value(
            name="qdt",
            func=self.get_qdt,
            dependencies=[self._splitting, self._dt, self._inmts],
        )  # positions
        self._qdt_on_m = depend_array(
            name="qdt_on_m",
            value=np.zeros(3 * self.beads.natoms),
            func=lambda: self.qdt / dstrip(self.beads.m3)[0],
        )
        self._pdt = depend_array(
            name="pdt",
            func=self.get_pdt,
            value=np.zeros(len(self.nmts)),
            dependencies=[self._splitting, self._dt, self._nmts],
        )  # momenta
        self._tdt = depend_value(
            name="tdt",
            func=self.get_tdt,
            dependencies=[self._splitting, self._dt, self._nmts],
        )  # thermostat

        dpipe(self._qdt, self.nm._dt)
        dpipe(self._dt, self.barostat._dt)
        dpipe(self._qdt, self.barostat._qdt)
        dpipe(self._pdt, self.barostat._pdt)
        dpipe(self._tdt, self.barostat._tdt)
        dpipe(self._tdt, self.thermostat._dt)

        if motion.enstype == "sc" or motion.enstype == "scnpt":
            # coefficients to get the (baseline) trotter to sc conversion
            self.coeffsc = np.ones((self.beads.nbeads, 3 * self.beads.natoms), float)
            self.coeffsc[::2] /= -3.0
            self.coeffsc[1::2] /= 3.0

        # check stress tensor
        self._stresscheck = True

    def pstep(self):
        """Dummy momenta propagator which does nothing."""
        pass

    def qcstep(self):
        """Dummy centroid position propagator which does nothing."""
        pass

    def step(self, step=None):
        """Dummy simulation time step which does nothing."""
        pass

    def pconstraints(self):
        """This removes the centre of mass contribution to the kinetic energy.

        Calculates the centre of mass momenta, then removes the mass weighted
        contribution from each atom. If the ensemble defines a thermostat, then
        the contribution to the conserved quantity due to this subtraction is
        added to the thermostat heat energy, as it is assumed that the centre of
        mass motion is due to the thermostat.

        If there is a choice of thermostats, the thermostat
        connected to the centroid is chosen.
        """

        if self.fixcom:
            na3 = self.beads.natoms * 3
            nb = self.beads.nbeads
            p = dstrip(self.beads.p)
            m = dstrip(self.beads.m3)[:, 0:na3:3]
            M = self.beads[0].M
            Mnb = M * nb

            dens = 0
            for i in range(3):
                pcom = p[:, i:na3:3].sum()
                dens += pcom**2
                pcom /= Mnb
                self.beads.p[:, i:na3:3] -= m * pcom

            self.ensemble.eens += dens * 0.5 / Mnb

        if len(self.fixatoms) > 0:
            for bp in self.beads.p:
                m = dstrip(self.beads.m)
                self.ensemble.eens += 0.5 * np.dot(
                    bp[self.fixatoms * 3], bp[self.fixatoms * 3] / m[self.fixatoms]
                )
                self.ensemble.eens += 0.5 * np.dot(
                    bp[self.fixatoms * 3 + 1],
                    bp[self.fixatoms * 3 + 1] / m[self.fixatoms],
                )
                self.ensemble.eens += 0.5 * np.dot(
                    bp[self.fixatoms * 3 + 2],
                    bp[self.fixatoms * 3 + 2] / m[self.fixatoms],
                )
                bp[self.fixatoms * 3] = 0.0
                bp[self.fixatoms * 3 + 1] = 0.0
                bp[self.fixatoms * 3 + 2] = 0.0


dproperties(
    DummyIntegrator,
    ["splitting", "nmts", "dt", "inmts", "nmtslevels", "qdt", "pdt", "tdt", "qdt_on_m"],
)


class NVEIntegrator(DummyIntegrator):
    """Integrator object for constant energy simulations.

    Has the relevant conserved quantity and normal mode propagator for the
    constant energy ensemble. Note that a temperature of some kind must be
    defined so that the spring potential can be calculated.

    Attributes:
        ptime: The time taken in updating the velocities.
        qtime: The time taken in updating the positions.
        ttime: The time taken in applying the thermostat steps.

    Depend objects:
        econs: Conserved energy quantity. Depends on the bead kinetic and
            potential energy, and the spring potential energy.
    """

    def pstep(self, level=0):
        """Velocity Verlet momentum propagator."""

        # halfdt/alpha
        self.beads.p[:] += dstrip(self.forces.forces_mts(level)) * self.pdt[level]
        if level == 0 and self.ensemble.has_bias:  # adds bias in the outer loop
            self.beads.p[:] += dstrip(self.bias.f) * self.pdt[level]

    def qcstep(self):
        """Velocity Verlet centroid position propagator."""
        # dt/inmts
        self.nm.qnm[0, :] += dstrip(self.nm.pnm)[0, :] * dstrip(self.qdt_on_m)

    # now the idea is that for BAOAB the MTS should work as follows:
    # take the BAB MTS, and insert the O in the very middle. This might imply breaking a A step in two, e.g. one could have
    # Bbabb(a/2) O (a/2)bbabB
    def mtsprop_ba(self, index):
        """Recursive MTS step"""

        mk = int(self.nmts[index] / 2)

        for i in range(mk):  # do nmts/2 full sub-steps
            self.pstep(index)
            self.pconstraints()
            if index == self.nmtslevels - 1:
                # call Q propagation for dt/alpha at the inner step
                self.qcstep()
                self.nm.free_qstep()
                self.qcstep()
                self.nm.free_qstep()

            else:
                self.mtsprop(index + 1)

            self.pstep(index)
            self.pconstraints()

        if self.nmts[index] % 2 == 1:
            # propagate p for dt/2alpha with force at level index
            self.pstep(index)
            self.pconstraints()
            if index == self.nmtslevels - 1:
                # call Q propagation for dt/alpha at the inner step
                self.qcstep()
                self.nm.free_qstep()
            else:
                self.mtsprop_ba(index + 1)

    def mtsprop_ab(self, index):
        """Recursive MTS step"""

        if self.nmts[index] % 2 == 1:
            if index == self.nmtslevels - 1:
                # call Q propagation for dt/alpha at the inner step
                self.qcstep()
                self.nm.free_qstep()
            else:
                self.mtsprop_ab(index + 1)

            # propagate p for dt/2alpha with force at level index
            self.pstep(index)
            self.pconstraints()

        for i in range(int(self.nmts[index] / 2)):  # do nmts/2 full sub-steps
            self.pstep(index)
            self.pconstraints()
            if index == self.nmtslevels - 1:
                # call Q propagation for dt/alpha at the inner step
                self.qcstep()
                self.nm.free_qstep()
                self.qcstep()
                self.nm.free_qstep()
            else:
                self.mtsprop(index + 1)

            self.pstep(index)
            self.pconstraints()

    def mtsprop(self, index):
        # just calls the two pieces together
        self.mtsprop_ba(index)
        self.mtsprop_ab(index)

    def step(self, step=None):
        """Does one simulation time step."""

        self.mtsprop(0)


class NVTIntegrator(NVEIntegrator):
    """Integrator object for constant temperature simulations.

    Has the relevant conserved quantity and normal mode propagator for the
    constant temperature ensemble. Contains a thermostat object containing the
    algorithms to keep the temperature constant.

    Attributes:
        thermostat: A thermostat object to keep the temperature constant.
    """

    def tstep(self):
        """Velocity Verlet thermostat step"""

        self.thermostat.step()

    def step(self, step=None):
        """Does one simulation time step."""

        if self.splitting == "obabo":
            # thermostat is applied for dt/2
            self.tstep()
            self.pconstraints()

            # forces are integerated for dt with MTS.
            self.mtsprop(0)

            # thermostat is applied for dt/2
            self.tstep()
            self.pconstraints()

        elif self.splitting == "baoab":
            self.mtsprop_ba(0)
            # thermostat is applied for dt
            self.tstep()
            self.pconstraints()
            self.mtsprop_ab(0)


class NVTCCIntegrator(NVTIntegrator):
    """Integrator object for constant temperature simulations with constrained centroid.

    Has the relevant conserved quantity and normal mode propagator for the
    constant temperature ensemble. Contains a thermostat object containing the
    algorithms to keep the temperature constant.

    Attributes:
        thermostat: A thermostat object to keep the temperature constant.
    """

    def pstep(self):
        """Velocity Verlet momenta propagator."""

        # propagates in NM coordinates
        self.nm.pnm += dstrip(self.nm.fnm) * (self.dt * 0.5)
        # self.beads.p += dstrip(self.forces.f)*(self.dt*0.5)
        # also adds the bias force
        # self.beads.p += dstrip(self.bias.f)*(self.dt*0.5)

    def step(self, step=None):
        """Does one simulation time step."""

        self.thermostat.step()
        self.pconstraints()
        # NB we only have to take into account the energy balance of zeroing centroid velocity when we had added energy through the thermostat
        self.ensemble.eens += 0.5 * np.dot(
            self.nm.pnm[0], self.nm.pnm[0] / self.nm.dynm3[0]
        )
        self.nm.pnm[0, :] = 0.0

        self.pstep()
        self.nm.pnm[0, :] = 0.0
        self.pconstraints()

        # self.qcstep() # for the moment I just avoid doing the centroid step.
        self.nm.free_qstep()

        self.pstep()
        self.nm.pnm[0, :] = 0.0
        self.pconstraints()

        self.thermostat.step()
        self.ensemble.eens += 0.5 * np.dot(
            self.nm.pnm[0], self.nm.pnm[0] / self.nm.dynm3[0]
        )
        self.nm.pnm[0, :] = 0.0
        self.pconstraints()


class NPTIntegrator(NVTIntegrator):
    """Integrator object for constant pressure simulations.

    Has the relevant conserved quantity and normal mode propagator for the
    constant pressure ensemble. Contains a thermostat object containing the
    algorithms to keep the temperature constant, and a barostat to keep the
    pressure constant.
    """

    # should be enough to redefine these functions, and the step() from NVTIntegrator should do the trick

    def pstep(self, level=0):
        """Velocity Verlet monemtum propagator."""

        if self._stresscheck and np.array_equiv(
            dstrip(self.forces.vir), np.zeros(len(self.forces.vir))
        ):
            warning(
                "Forcefield returned a zero stress tensor. NPT simulation will likely make no sense",
                verbosity.low,
            )
            if verbosity.medium:
                raise ValueError(
                    "Zero stress terminates simulation for medium verbosity and above."
                )

        self._stresscheck = False

        self.barostat.pstep(level)
        super(NPTIntegrator, self).pstep(level)
        # self.pconstraints()

    def qcstep(self):
        """Velocity Verlet centroid position propagator."""

        self.barostat.qcstep()

    def tstep(self):
        """Velocity Verlet thermostat step"""

        self.thermostat.step()
        self.barostat.thermostat.step()
        # self.pconstraints()


class NSTIntegrator(NPTIntegrator):
    """Ensemble object for constant pressure simulations.

    Has the relevant conserved quantity and normal mode propagator for the
    constant pressure ensemble. Contains a thermostat object containing the
    algorithms to keep the temperature constant, and a barostat to keep the
    pressure constant.

    Attributes:
    barostat: A barostat object to keep the pressure constant.

    Depend objects:
    econs: Conserved energy quantity. Depends on the bead and cell kinetic
    and potential energy, the spring potential energy, the heat
    transferred to the beads and cell thermostat, the temperature and
    the cell volume.
    pext: External pressure.
    """


class SCIntegrator(NVTIntegrator):
    """Integrator object for constant temperature simulations.

    Has the relevant conserved quantity and normal mode propagator for the
    constant temperature ensemble. Contains a thermostat object containing the
    algorithms to keep the temperature constant.

    Attributes:
        thermostat: A thermostat object to keep the temperature constant.

    Depend objects:
        econs: Conserved energy quantity. Depends on the bead kinetic and
            potential energy, the spring potential energy and the heat
            transferred to the thermostat.
    """

    def bind(self, mover):
        """Binds ensemble beads, cell, bforce, bbias and prng to the dynamics.

        This takes a beads object, a cell object, a forcefield object and a
        random number generator object and makes them members of the ensemble.
        It also then creates the objects that will hold the data needed in the
        ensemble algorithms and the dependency network. Note that the conserved
        quantity is defined in the init, but as each ensemble has a different
        conserved quantity the dependencies are defined in bind.

        Args:
        beads: The beads object from whcih the bead positions are taken.
        nm: A normal modes object used to do the normal modes transformation.
        cell: The cell object from which the system box is taken.
        bforce: The forcefield object from which the force and virial are
            taken.
        prng: The random number generator object which controls random number
            generation.
        """

        super(SCIntegrator, self).bind(mover)
        self.ensemble.add_econs(self.forces._potsc)
        self.ensemble.add_xlpot(self.forces._potsc)

    def pstep(self, level=0):
        """Velocity Verlet monemtum propagator."""

        if level == 0:
            # bias goes in the outer loop
            self.beads.p += dstrip(self.bias.f) * self.pdt[level]
        # just integrate the Trotter force scaled with the SC coefficients, which is a cheap approx to the SC force
        self.beads.p += (
            self.forces.forces_mts(level)
            * (1.0 + self.forces.coeffsc_part_1)
            * self.pdt[level]
        )

    def step(self, step=None):
        # the |f|^2 term is considered to be slowest (for large enough P) and is integrated outside everything.
        # if nmts is not specified, this is just the same as doing the full SC integration

        if self.splitting == "obabo":
            # thermostat is applied for dt/2
            self.tstep()
            self.pconstraints()

            # forces are integerated for dt with MTS.
            self.beads.p += dstrip(self.forces.fsc_part_2) * self.dt * 0.5
            self.mtsprop(0)
            self.beads.p += dstrip(self.forces.fsc_part_2) * self.dt * 0.5

            # thermostat is applied for dt/2
            self.tstep()
            self.pconstraints()

        elif self.splitting == "baoab":
            self.beads.p += dstrip(self.forces.fsc_part_2) * self.dt * 0.5
            self.mtsprop_ba(0)
            # thermostat is applied for dt
            self.tstep()
            self.pconstraints()
            self.mtsprop_ab(0)
            self.beads.p += dstrip(self.forces.fsc_part_2) * self.dt * 0.5


class SCNPTIntegrator(SCIntegrator):
    """Integrator object for constant pressure Suzuki-Chin simulations.

    Has the relevant conserved quantity and normal mode propagator for the
    constant pressure ensemble. Contains a thermostat object containing the
    algorithms to keep the temperature constant, and a barostat to keep the
    pressure constant.
    """

    # should be enough to redefine these functions, and the step() from NVTIntegrator should do the trick
    def pstep(self, level=0):
        """Velocity Verlet monemtum propagator."""

        if self._stresscheck and np.array_equiv(
            dstrip(self.forces.vir), np.zeros(len(self.forces.vir))
        ):
            warning(
                "Forcefield returned a zero stress tensor. NPT simulation will likely make no sense",
                verbosity.low,
            )
            if verbosity.medium:
                raise ValueError(
                    "Zero stress terminates simulation for medium verbosity and above."
                )

        self._stresscheck = False

        self.barostat.pstep(level)
        super(SCNPTIntegrator, self).pstep(level)

    def qcstep(self):
        """Velocity Verlet centroid position propagator."""

        self.barostat.qcstep()

    def tstep(self):
        """Velocity Verlet thermostat step"""

        self.thermostat.step()
        self.barostat.thermostat.step()

    def step(self, step=None):
        # the |f|^2 term is considered to be slowest (for large enough P) and is integrated outside everything.
        # if nmts is not specified, this is just the same as doing the full SC integration

        if self.splitting == "obabo":
            # thermostat is applied for dt/2
            self.tstep()
            self.pconstraints()

            # forces are integerated for dt with MTS.
            self.barostat.pscstep()
            self.beads.p += dstrip(self.forces.fsc_part_2) * self.dt * 0.5
            self.mtsprop(0)
            self.barostat.pscstep()
            self.beads.p += dstrip(self.forces.fsc_part_2) * self.dt * 0.5

            # thermostat is applied for dt/2
            self.tstep()
            self.pconstraints()

        elif self.splitting == "baoab":
            self.barostat.pscstep()
            self.beads.p += dstrip(self.forces.fsc_part_2) * self.dt * 0.5
            self.mtsprop_ba(0)
            # thermostat is applied for dt
            self.tstep()
            self.pconstraints()
            self.mtsprop_ab(0)
            self.barostat.pscstep()
            self.beads.p += dstrip(self.forces.fsc_part_2) * self.dt * 0.5


class EDAIntegrator(DummyIntegrator):
    """Integrator object for simulations using the Electric Dipole Approximation (EDA)
    when an external electric field is applied.
    """

    def __init__(self):
        super().__init__()

    def bind(self, motion):
        """bind variables"""
        super().bind(motion)

        self._time = self.eda._time
        self._mts_time = self.eda._mts_time

        dep = [
            self._time,
            self._mts_time,
            self.eda.Born_Charges._bec,
            self.eda.Electric_Field._Efield,
        ]
        self._EDAforces = depend_array(
            name="EDAforces",
            func=self._eda_forces,
            value=np.zeros((self.beads.nbeads, self.beads.natoms * 3)),
            dependencies=dep,
        )
        pass

    def pstep(self, level=0):
        """Velocity Verlet momentum propagator."""
        self.beads.p += self.EDAforces * self.pdt[level]
        if dstrip(self.mts_time) == dstrip(self.time):
            # it's the first time that 'pstep' is called
            # then we need to update 'mts_time'
            self.mts_time += dstrip(self.dt)
            # the next time this condition will be 'False'
            # so we will avoid to re-compute the EDAforces
        pass

    def _eda_forces(self):
        """Compute the EDA contribution to the forces due to the polarization"""
        Z = dstrip(self.eda.Born_Charges.bec)
        E = dstrip(self.eda.Electric_Field.Efield)
        forces = Constants.e * Z @ E
        return forces

    def step(self, step=None):
        if len(self.nmts) > 1:
            raise ValueError(
                "EDAIntegrator is not implemented with the Multiple Time Step algorithm (yet)."
            )
        super().step(step)


dproperties(EDAIntegrator, ["EDAforces", "mts_time", "time"])


class EDANVEIntegrator(EDAIntegrator, NVEIntegrator):
    """Integrator object for simulations with constant Number of particles, Volume, and Energy (NVE)
    using the Electric Dipole Approximation (EDA) when an external electric field is applied.
    """

    def pstep(self, level):
        # NVEIntegrator does not use 'super()' within 'pstep'
        # then we can not use 'super()' here.
        # We need to call the 'pstep' methods explicitly.
        NVEIntegrator.pstep(
            self, level
        )  # the driver is called here: add nuclear and electronic forces (DFT)
        EDAIntegrator.pstep(self, level)  # add the driving forces, i.e. q_e Z @ E(t)
        pass
