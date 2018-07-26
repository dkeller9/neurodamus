from __future__ import absolute_import
from .configuration import Neuron_Stdrun_Defaults
from .configuration import GlobalConfig
from ..utils import classproperty


#
# Singleton, instantiated right below
#
class _Neuron(object):
    """
    A wrapper over the neuron simulator.
    """
    # The neuron hoc interpreter
    # We dont import it at module-level to avoid starting neuron
    _h = None
    _mods_loaded = set()

    # No new attributes. __setattr__ can rely on it
    __slots__ = ()

    @classproperty
    def h(cls):
        """The neuron hoc interpreter, initializing if needed
        """
        return cls._h or cls._init()

    @classmethod
    def _init(cls):
        """Initializes the Neuron simulator"""
        if cls._h is None:
            if GlobalConfig.use_mpi:
                pass
                # Currently _init_mpi is based on MPI4Py which is problematic in bbp5
                # Please start with nrniv -mpi -python
                # _init_mpi()
            from neuron import h
            from neuron import nrn
            cls._h = h
            cls.Section = nrn.Section
            cls.Segment = nrn.Segment
            h.load_file("stdrun.hoc")
            h("objref nil")
            h.init()
        return cls._h

    @classmethod
    def load_hoc(cls, mod_name):
        """Loads a hoc module, available in the path.
        E.g.: Neuron.load_mod("loadbal")
        """
        if mod_name in cls._mods_loaded:
            return
        h = (cls._h or cls._init())
        mod_filename = mod_name + ".hoc"
        if not h.load_file(mod_filename):
            raise RuntimeError("Cant load HOC library {}. Consider checking HOC_LIBRARY_PATH"
                               .format(mod_filename))
        cls._mods_loaded.add(mod_name)

    @classmethod
    def require(cls, *hoc_mods):
        for mod in hoc_mods:
            cls.load_hoc(mod)
        return cls._h

    @classmethod
    def load_dll(cls, dll_path):
        """Loads a Neuron mod file (typically an .so file in linux)"""
        h = (cls._h or cls._init())
        rc = h.nrn_load_dll(dll_path)
        if rc == 0:
            raise RuntimeError("Cant load MOD dll {}. Please check LD path and dependencies"
                               .format(dll_path))

    @classmethod
    def run_sim(cls, t_stop, *monitored_sections, **params):
        """A helper to run the simulation, recording the Voltage in the specified cell sections.
        Args:
            t_stop: Stop time
            *monitored_sections: Cell sections to be probed.
            **params: Custom simulation parameters

        Returns: A simulation object
        """
        cls._h or cls._init()
        sim = Simulation(**params)
        for sec in monitored_sections:
            sim.record_activity(sec)
        sim.run(t_stop)
        return sim

    # Properties that are not found here are get / set
    # directly in neuron.h
    def __getattr__(self, item):
        return getattr(self.h, item)

    def __setattr__(self, key, value):
        try:
            object.__setattr__(self, key, value)
        except AttributeError:
            setattr(self.h, key, value)

    # public shortcuts
    HocEntity = None   # type: HocEntity
    Simulation = None  # type: Simulation
    LoadBalance = None  # type: type
    Section = None
    Segment = None


# The singleton
Neuron = _Neuron()


class _MPI:
    _size = 1
    _rank = 0
    _pnm = None

    @classmethod
    def _init_pnm(cls):
        if cls._pnm is not None:
            return
        Neuron.load_hoc("netparmpi")
        cls._pnm = pnm = Neuron.ParallelNetManager(0)
        cls._rank = int(pnm.pc.id())
        cls._size = int(pnm.pc.nhost())

    @property
    def pnm(self):
        self._init_pnm()
        return self._pnm

    @property
    def size(self):
        self._init_pnm()
        return self._size

    cpu_count = size

    @property
    def rank(self):
        self._init_pnm()
        return self._rank

    def __getattr__(self, name):
        return getattr(self._pnm.pc, name)


# A singleton
MPI = _MPI()


class HocEntity(object):
    _hoc_cls = None
    _hoc_obj = None
    _hoc_cldef = """
begintemplate {cls_name}
endtemplate {cls_name}
"""

    def __new__(cls, *args, **kw):
        if cls is HocEntity:
            raise TypeError("HocEntity must be subclassed")
        if cls._hoc_cls is None:
            h = Neuron.h
            # Create a HOC template to be able to use as context
            h(cls._hoc_cldef.format(cls_name=cls.__name__))
            cls._hoc_cls = getattr(h, cls.__name__)

        o = object.__new__(cls)
        o._hoc_obj = cls._hoc_cls()
        return o

    @property
    def h(self):
        return self._hoc_obj


class Simulation:
    # Some defaults from stdrun
    v_init = Neuron_Stdrun_Defaults.v_init  # -65V

    def __init__(self, **args):
        args.setdefault("v_init", self.v_init)
        self.args = args
        self.t_vec = None
        self.recordings = {}

    def run(self, t_stop):
        h = Neuron.h
        self.t_vec = h.Vector()  # Time stamp vector
        self.t_vec.record(h._ref_t)

        Neuron.h.tstop = t_stop
        for key, val in self.args.items():
            setattr(Neuron.h, key, val)
        Neuron.h.run()

    def run_continue(self, t_stop):
        Neuron.h.continuerun(t_stop)

    def record_activity(self, section, rel_pos=0.5):
        if isinstance(section, Neuron.Segment):
            segment = section
            name = str(segment.sec)
        else:
            segment = section(rel_pos)
            name = section.name()

        rec_vec = Neuron.h.Vector()
        rec_vec.record(segment._ref_v)
        self.recordings[name] = rec_vec

    def get_voltages_at(self, section):
        return self.recordings[section.name()]

    def plot(self):
        try:
            from matplotlib import pyplot
        except Exception:
            print("Matplotlib is not installed. Please install pyneurodamus[full]")
            return None
        if len(self.recordings) == 0:
            print("No recording sections defined")
            return None
        if not self.t_vec:
            print("No Simulation data. Please run it first.")
            return None

        fig = pyplot.figure()
        ax = fig.add_subplot(1, 1, 1)  # (nrows, ncols, axnum)
        for name, y in self.recordings.items():
            ax.plot(self.t_vec, y, label=name)
        ax.legend()
        fig.show()


class LoadBalance(object):
    """Wrapper of the load balance Hoc Module.
    """
    def __init__(self):
        self._lb = Neuron.h.LoadBalance()

    def create_mcomplex(self):
        self._lb.ExperimentalMechComplex("StdpWA", "extracel", "HDF5", "Report", "Memory", "ASCII")

    def read_mcomplex(self):
        self._lb.read_mcomplex()

    def __getattr__(self, item):
        return getattr(self._lb, item)


# shortcuts
_Neuron.HocEntity = HocEntity
_Neuron.Simulation = Simulation
_Neuron.LoadBalance = LoadBalance
