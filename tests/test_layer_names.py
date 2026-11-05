from kipy.proto.board.board_types_pb2 import BoardLayer

from z2ems.ports import cu_layer_name


class RenamedBoard:
    """KiCad returns the user's layer names ('In1_GND'); the engine needs the canonical ones."""
    names = {BoardLayer.BL_F_Cu: "Top", BoardLayer.BL_In1_Cu: "In1_GND", BoardLayer.BL_B_Cu: "Bottom",
             BoardLayer.BL_User_1: "Z2EMS.Ports"}

    def get_layer_name(self, layer):
        return self.names[layer]


def test_copper_layers_keep_canonical_names():
    b = RenamedBoard()
    assert cu_layer_name(b, BoardLayer.BL_F_Cu) == "F.Cu"
    assert cu_layer_name(b, BoardLayer.BL_In1_Cu) == "In1.Cu"
    assert cu_layer_name(b, BoardLayer.BL_B_Cu) == "B.Cu"


def test_other_layers_use_display_name():
    assert cu_layer_name(RenamedBoard(), BoardLayer.BL_User_1) == "Z2EMS.Ports"
