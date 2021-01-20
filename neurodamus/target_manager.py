import itertools
import logging
import os.path
from .core.configuration import GlobalConfig, find_input_file
from .core import MPI, NeurodamusCore as Nd
from .utils.logging import log_verbose


class TargetSpec:
    """Definition of a new-style target, accounting for multipopulation
    """

    def __init__(self, target_name):
        """Initialize a target specification

        Args:
            target_name: the target name. For specifying a population use
                the format ``population:target_name``
        """
        if target_name and ':' in target_name:
            self.population, self.name = target_name.split(':')
        else:
            self.name = target_name
            self.population = None
        if self.name == "":
            self.name = None

    def __str__(self):
        return self.name if self.population is None \
            else "{}:{}".format(self.population, self.name)

    def __bool__(self):
        return bool(self.name)

    @property
    def simple_name(self):
        if self.name is None:
            return "_ALL_"
        return self.__str__().replace(":", "_")

    def matches(self, pop, target_name):
        return pop == self.population and target_name == self.name

    def match_filter(self, pop, target_name, is_base_population=False):
        return ((self.population == pop or (is_base_population and self.population is None))
                and target_name in (None, self.name))

    def __eq__(self, other):
        return self.matches(other.population, other.name)


class TargetManager:
    def __init__(self, run_conf):
        self._run_conf = run_conf
        self.parser = Nd.TargetParser()
        self.hoc = None  # The hoc level target manager
        # self._targets_fq = {}

    def load_targets(self, circuit):
        """Provided that the circuit location is known and whether a user.target file has been
        specified, load any target files via a TargetParser.
        Note that these will be moved into a TargetManager after the cells have been distributed,
        instantiated, and potentially split.
        """
        run_conf = self._run_conf
        if MPI.rank == 0:
            self.parser.isVerbose = 1

        if circuit.CircuitPath:
            start_target_file = os.path.join(circuit.CircuitPath, "start.target")
            if not os.path.isfile(start_target_file):
                logging.warning("start.target not available! Check circuit.")
            else:
                self.parser.open(start_target_file)

        if "TargetFile" in run_conf:
            user_target = find_input_file(run_conf["TargetFile"])
            self.parser.open(user_target, True)

        if MPI.rank == 0:
            logging.info(" => Loaded %d targets", self.parser.targetList.count())
            if GlobalConfig.verbosity >= 3:
                self.parser.printCellCounts()

    def get_target(self, target_name):
        return self.parser.getTarget(target_name)

    def init_hoc_manager(self, cell_manager):
        # give a TargetManager the TargetParser's completed targetList
        self.hoc = Nd.TargetManager(self.parser.targetList, cell_manager)

    def generate_subtargets(self, target_name, n_parts):
        """To facilitate CoreNeuron data generation, we allow users to use ModelBuildingSteps to
        indicate that the CircuitTarget should be split among multiple, smaller targets that will
        be built step by step.

        Returns:
            list with generated targets, or empty if no splitting was done
        """
        if not n_parts or n_parts == 1:
            return False

        target = self.parser.getTarget(target_name)
        allgids = target.completegids()
        new_targets = []

        for cycle_i in range(n_parts):
            target = Nd.Target()
            target.name = "{}_{}".format(target_name, cycle_i)
            new_targets.append(target)
            self.parser.updateTargetList(target)

        target_looper = itertools.cycle(new_targets)
        for gid in allgids.x:
            target = next(target_looper)
            target.gidMembers.append(gid)

        return new_targets

    def get_target_info(self, target_spec, verbose=False):
        """Count the total number of the target cells, and get the max gid
           if CircuitTarget is not specified in the configuration, use Mosaic target
        """
        target_name = target_spec.name
        if target_name is None:
            logging.warning("No circuit target was set. Assuming Mosaic")
            target_name = "Mosaic"
        target_obj = self.get_target(target_name)
        cell_count = target_obj.getCellCount()
        all_gids = target_obj.completegids()
        if verbose:
            logging.info("CIRCUIT: Population: %s, Target: %s (%d Cells)",
                         target_spec.population or "(default)",
                         target_spec.name or "(Mosaic)",
                         cell_count)
        return cell_count, max(all_gids) if all_gids else 0

    def get_target_points(self, target, cell_manager, cell_use_compartment_cast=True):
        """Helper to retrieve the points of a target.
        If target is a cell then uses compartmentCast to obtain its points.
        Otherwise returns the result of calling getPointList directly on the target.

        Args:
            target: The target name or object (faster)
            manager: The cell manager to access gids and metype infos
            cell_use_compartment_cast: if enabled (default) will use target_manager.compartmentCast
                to get the point list.

        Returns: The target list of points
        """
        if isinstance(target, str):
            target = self.get_target(target)
        if target.isCellTarget() and cell_use_compartment_cast:
            target = self.hoc.compartmentCast(target, "")
        log_verbose("Using cell manager with cells offset by %d", cell_manager.gid_offset)
        return target.getPointList(cell_manager)
