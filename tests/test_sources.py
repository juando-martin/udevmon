import unittest

from udevmon.events import CONNECTED, DISCONNECTED, MOUNTED, REMOVED, UNMOUNTED
from udevmon.sources import udev as U
from udevmon.sources import udisks as D


class UdevTests(unittest.TestCase):
    def props(self, **raw):
        return U.normalize(raw, syspath="/sys/x/1-2", subsystem="usb", devtype="usb_device",
                           devnode="/dev/bus/usb/001/005", sys_name="1-2")

    def test_normalize(self):
        p = self.props(ID_VENDOR_ID="1050", ID_MODEL_ID="0407", ID_SERIAL_SHORT="S9x7",
                       ID_VENDOR_FROM_DATABASE="Yubico", ID_MODEL="YubiKey")
        self.assertEqual((p["vendor_id"], p["product_id"], p["serial"]), ("1050", "0407", "S9x7"))
        self.assertEqual((p["vendor"], p["model"]), ("Yubico", "YubiKey"))
        self.assertEqual(p["port_path"], "1-2")
        self.assertEqual(p["udev_id_serial_short"], "S9x7")

    def test_product_fallback(self):
        p = self.props(PRODUCT="46d/825/1")
        self.assertEqual((p["vendor_id"], p["product_id"]), ("046d", "0825"))

    def test_remove_uses_cache(self):
        src = U.UdevSource()
        add = src.handle("add", self.props(ID_VENDOR_ID="1050", ID_MODEL_ID="0407",
                                          ID_SERIAL_SHORT="S9x7"))
        self.assertEqual(add.kind, CONNECTED)
        rem = src.handle("remove", self.props())      # remove event without ID_* props
        self.assertEqual(rem.kind, DISCONNECTED)
        self.assertEqual(rem.endpoint.props["serial"], "S9x7")
        self.assertIsNone(src.handle("change", self.props()))

    def test_filters(self):
        self.assertEqual(U.parse_filters(["usb/usb_device", "input"]),
                         [("usb", "usb_device"), ("input", None)])


FS = "org.freedesktop.UDisks2.Filesystem"
BLK = "org.freedesktop.UDisks2.Block"
P = "/org/freedesktop/UDisks2/block_devices/sdb1"
DRV = "/org/freedesktop/UDisks2/drives/X"


def managed(mounted=False):
    return {
        DRV: {D.I_DRIVE: {"Vendor": "SanDisk", "Model": "Cruzer", "Serial": "SER1"}},
        P: {BLK: {"IdUUID": "U-1", "IdLabel": "BACKUP", "IdType": "ext4",
                  "Device": list(b"/dev/sdb1\0"), "Drive": DRV},
            D.I_PART: {"UUID": "PU-1"},
            FS: {"MountPoints": [list(b"/run/media/u/BACKUP\0")] if mounted else []}},
    }


class UdisksTrackerTests(unittest.TestCase):
    def test_mount_unmount_cycle(self):
        t = D.UdisksTracker()
        t.load(managed())
        ev = t.properties_changed(P, FS, {"MountPoints": [list(b"/run/media/u/BACKUP\0")]})
        self.assertEqual([e.kind for e in ev], [MOUNTED])
        props = ev[0].endpoint.props
        self.assertEqual(props["mountpoint"], "/run/media/u/BACKUP")
        self.assertEqual((props["fs_uuid"], props["fs_label"], props["part_uuid"],
                          props["devnode"], props["drive_serial"]),
                         ("U-1", "BACKUP", "PU-1", "/dev/sdb1", "SER1"))
        self.assertEqual(t.properties_changed(P, FS, {"MountPoints": [list(b"/run/media/u/BACKUP\0")]}), [])
        ev = t.properties_changed(P, FS, {"MountPoints": []})
        self.assertEqual([e.kind for e in ev], [UNMOUNTED])
        self.assertEqual(ev[0].endpoint.props["mountpoint"], "/run/media/u/BACKUP")

    def test_startup_state_fires_nothing_then_unmount(self):
        t = D.UdisksTracker()
        t.load(managed(mounted=True))
        ev = t.properties_changed(P, FS, {"MountPoints": []})
        self.assertEqual([e.kind for e in ev], [UNMOUNTED])

    def test_added_already_mounted_and_surprise_removal(self):
        t = D.UdisksTracker()
        t.load({})
        m = managed(mounted=True)
        self.assertEqual(t.interfaces_added(DRV, m[DRV]), [])
        ev = t.interfaces_added(P, m[P])
        self.assertEqual([e.kind for e in ev], [MOUNTED])
        ev = t.interfaces_removed(P, [FS, BLK, D.I_PART])
        self.assertEqual([e.kind for e in ev], [UNMOUNTED, REMOVED])       # a yank: both events
        self.assertNotIn(P, t.objects)

    def test_filesystems_listing(self):
        t = D.UdisksTracker()
        t.load(managed())
        fs = t.filesystems()
        self.assertEqual(len(fs), 1)
        self.assertNotIn("mountpoint", fs[0].props)

    def test_bytes_and_int_list_decoding(self):
        self.assertEqual(D._str(b"/a\0"), "/a")
        self.assertEqual(D._str(list(b"/a\0")), "/a")
        self.assertEqual(D._mountpoints([b"/a\0", list(b"/b\0")]), ["/a", "/b"])


if __name__ == "__main__":
    unittest.main()
