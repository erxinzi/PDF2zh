import unittest
from pathlib import Path
from xml.etree import ElementTree

from utils.display import layout_dimensions


class DisplayTests(unittest.TestCase):
    def test_default_window_has_more_working_space(self):
        result = layout_dimensions(1920, 1040, 96)
        self.assertEqual((result["width"], result["height"]), (1280, 860))

    def test_dpi_scaling_keeps_window_and_minimum_inside_the_work_area(self):
        for width, height in ((1366, 728), (1920, 1040), (2560, 1400), (3840, 2080)):
            for dpi in (96, 120, 144, 192):
                result = layout_dimensions(width, height, dpi)
                self.assertLess(result["width"], width)
                self.assertLess(result["height"], height)
                self.assertLessEqual(result["min_width"], result["width"])
                self.assertLessEqual(result["min_height"], result["height"])

    def test_manifest_declares_per_monitor_v2_without_admin_permissions(self):
        manifest = Path(__file__).resolve().parents[1] / "windows.manifest"
        tree = ElementTree.parse(manifest)
        dpi = tree.find(".//{http://schemas.microsoft.com/SMI/2016/WindowsSettings}dpiAwareness")
        self.assertEqual(dpi.text, "PerMonitorV2,PerMonitor")
        privilege = tree.find(".//{urn:schemas-microsoft-com:asm.v3}requestedExecutionLevel")
        self.assertEqual(privilege.attrib["level"], "asInvoker")


if __name__ == "__main__":
    unittest.main()
