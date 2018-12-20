from pacman.model.graphs.machine import MachineVertex
from pacman.model.resources.resource_container import ResourceContainer
from pacman.model.resources.dtcm_resource import DTCMResource
from pacman.model.resources.sdram_resource import SDRAMResource
from pacman.model.resources.cpu_cycles_per_tick_resource \
    import CPUCyclesPerTickResource
from pacman.model.decorators.overrides import overrides
from pacman.executor.injection_decorator import inject_items

from data_specification.enums.data_type import DataType

from spinn_front_end_common.abstract_models.abstract_has_associated_binary \
    import AbstractHasAssociatedBinary
from spinn_front_end_common.abstract_models\
    .abstract_generates_data_specification \
    import AbstractGeneratesDataSpecification
from spinn_front_end_common.interface.buffer_management.buffer_models\
    .abstract_receive_buffers_to_host import AbstractReceiveBuffersToHost
from spinn_front_end_common.utilities import helpful_functions
from spinn_front_end_common.interface.buffer_management \
    import recording_utilities
from spinn_front_end_common.utilities.utility_objs import ExecutableType
from spinn_front_end_common.interface.profiling.profile_data \
    import ProfileData
from enum import Enum
import numpy

from spinn_front_end_common.abstract_models\
    .abstract_provides_n_keys_for_partition \
    import AbstractProvidesNKeysForPartition

from spinn_utilities.progress_bar import ProgressBar

from spinn_front_end_common.interface.profiling.abstract_has_profile_data \
    import AbstractHasProfileData
from spinn_front_end_common.interface.profiling import profile_utils

class DRNLVertex(
        MachineVertex, AbstractHasAssociatedBinary,
        AbstractGeneratesDataSpecification,
        AbstractProvidesNKeysForPartition,
        AbstractHasProfileData
        ):
    """ A vertex that runs the DRNL algorithm
    """
    # The number of bytes for the parameters
    _N_PARAMETER_BYTES = 10*4
    # The data type of each data element
    _DATA_ELEMENT_TYPE = DataType.FLOAT_32
    # The data type of the data count
    _DATA_COUNT_TYPE = DataType.UINT32
    # The numpy data type of each data element
    _NUMPY_DATA_ELEMENT_TYPE = numpy.single
    # The data type of the keys
    _KEY_ELEMENT_TYPE = DataType.UINT32
    # the data type of the coreID
    _COREID_TYPE = DataType.UINT32

    PROFILE_TAG_LABELS = {
        0: "TIMER",
        1: "DMA_READ",
        2: "INCOMING_SPIKE",
        3: "PROCESS_FIXED_SYNAPSES",
        4: "PROCESS_PLASTIC_SYNAPSES"}

    def __init__(self, ome,CF,delay,profile=True,data_partition_name="DRNLData",
            acknowledge_partition_name="DRNLDataAck"):
        """

        :param ome: The connected ome vertex
        """
        AbstractProvidesNKeysForPartition.__init__(self)
        MachineVertex.__init__(self, label="DRNL Node", constraints=None)
        self._ome = ome
        self._ome.register_processor(self)
        self._CF=CF
        self._fs=ome.fs
        self._delay=int(delay)
        self._mack=ome

        self._ihcan_vertices = list()
        self._ihcan_placements = list()
        self._mask = list()

        self._num_data_points = ome.n_data_points
        self._data_size = (
            self._num_data_points * self._DATA_ELEMENT_TYPE.size +
            self._DATA_COUNT_TYPE.size
        )

        self._sdram_usage = (
            self._N_PARAMETER_BYTES #+ self._data_size
        )

        self._data_partition_name = data_partition_name
        self._acknowledge_partition_name = acknowledge_partition_name

        # Set up for profiling
        self._profile = profile
        self._n_profile_samples = 10000
        self._process_profile_times = None

    def register_processor(self, ihcan_vertex):
        self._ihcan_vertices.append(ihcan_vertex)

    def register_parent_processor(self, mack):
        self._mack=mack

    def get_acknowledge_key(self, placement, routing_info):
        key = routing_info.get_first_key_from_pre_vertex(
            placement.vertex, self._acknowledge_partition_name)
        return key

    @property
    def n_data_points(self):
        return self._num_data_points

    @property
    def fs(self):
        return self._fs

    @property
    def data_partition_name(self):
        return self._data_partition_name

    @property
    def acknowledge_partition_name(self):
        return self._acknowledge_partition_name

    @property
    @overrides(MachineVertex.resources_required)
    def resources_required(self):
        sdram = self._N_PARAMETER_BYTES
        sdram += len(self._ihcan_vertices) * self._KEY_ELEMENT_TYPE.size
        if self._profile:
            sdram += profile_utils.get_profile_region_size(self._n_profile_samples)

        resources = ResourceContainer(
            dtcm=DTCMResource(0),
            sdram=SDRAMResource(sdram),
            cpu_cycles=CPUCyclesPerTickResource(0),
            iptags=[], reverse_iptags=[])
        return resources

    @overrides(AbstractHasAssociatedBinary.get_binary_file_name)
    def get_binary_file_name(self):
        return "SpiNNakEar_DRNL.aplx"

    @overrides(AbstractHasAssociatedBinary.get_binary_start_type)
    def get_binary_start_type(self):
        return ExecutableType.SYNC

    @overrides(AbstractProvidesNKeysForPartition.get_n_keys_for_partition)
    def get_n_keys_for_partition(self, partition, graph_mapper):
        return 4#2  # two for control IDs

    @overrides(AbstractHasProfileData.get_profile_data)
    def get_profile_data(self, transceiver, placement):
        if self._profile:
            profiles = profile_utils.get_profiling_data(
                1,
                self.PROFILE_TAG_LABELS, transceiver, placement)
            self._process_profile_times = profiles._tags['TIMER'][1]
        else:
            profiles=ProfileData(self.PROFILE_TAG_LABELS)
        return profiles

    @inject_items({
        "routing_info": "MemoryRoutingInfos",
        "tags": "MemoryTags",
        "placements": "MemoryPlacements"
    })

    @overrides(
        AbstractGeneratesDataSpecification.generate_data_specification,
        additional_arguments=["routing_info", "tags", "placements"])
    def generate_data_specification(
            self, spec, placement, routing_info, tags, placements):

        OME_placement=placements.get_placement_of_vertex(self._ome).p

        # Reserve and write the parameters region
        region_size = self._N_PARAMETER_BYTES
        region_size += (1 + len(self._ihcan_vertices)) * self._KEY_ELEMENT_TYPE.size
        spec.reserve_memory_region(0, region_size)
        if self._profile:
            #reserve profile region
            profile_utils.reserve_profile_region(
                spec, 1,
                self._n_profile_samples)

        spec.switch_write_focus(0)

        # Get the placement of the vertices and find out how many chips
        # are needed
        keys = list()
        for vertex in self._ihcan_vertices:
            ihcan_placement = placements.get_placement_of_vertex(vertex)
            self._ihcan_placements.append(ihcan_placement)
            key = routing_info.get_first_key_from_pre_vertex(
                vertex, self._acknowledge_partition_name)
            keys.append(key)
        keys.sort()

        # Write the data size in words
        spec.write_value(
            self._num_data_points * (float(self._DATA_ELEMENT_TYPE.size) / 4.0),
            data_type=self._DATA_COUNT_TYPE)

        # Write the OMECoreID
        spec.write_value(
            OME_placement, data_type=self._COREID_TYPE)

        # Write the CoreID
        spec.write_value(
            placement.p, data_type=self._COREID_TYPE)

        #Write the OMEAppID
        spec.write_value(
            0, data_type=self._COREID_TYPE)

        # Write the Acknowledge key
        spec.write_value(self._mack.get_acknowledge_key(
            placement, routing_info))

        # Write the key
        if len(keys)>0:
            data_key_orig = routing_info.get_routing_info_from_pre_vertex(
                self, self._data_partition_name).first_key
            data_key = routing_info.get_first_key_from_pre_vertex(
                self, self._data_partition_name)

            spec.write_value(data_key, data_type=DataType.UINT32)
        else:
            raise Exception("no drnl key generated!")

        # Write number of ihcans
        spec.write_value(
            len(self._ihcan_vertices), data_type=self._COREID_TYPE)

        # Write the centre frequency
        spec.write_value(self._CF,data_type=DataType.UINT32)

        # Write the delay
        spec.write_value(self._delay,data_type=DataType.UINT32)

        # Write the sampling frequency
        spec.write_value(self._fs,data_type=DataType.UINT32)

        #write the OME data key
        ome_data_key = routing_info.get_first_key_from_pre_vertex(
            self._ome, self._ome.data_partition_name)
        spec.write_value(ome_data_key, data_type=DataType.UINT32)

    #    print "DRNL OME placement=",OME_placement
   #     print "DRNL placement=",placement.p
        if self._profile:
            profile_utils.write_profile_region_data(
                spec, 1,
                self._n_profile_samples)

        # End the specification
        spec.end_specification()

    def read_samples(self, buffer_manager):
        """ Read back the samples
        """
        progress = ProgressBar(len(self._ihcan_placements), "Reading results")
        samples = list()
        for placement in self._ihcan_placements:

            # Read the data recorded
            samples.append(
                placement.vertex.read_samples(buffer_manager, placement))
            progress.update()
        progress.end()

        # Merge all the arrays
        return numpy.hstack(samples)