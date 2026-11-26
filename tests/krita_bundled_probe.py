"""Is the co-op engine really built into this Krita?  Run with the build's kritarunner:

    cd tests
    <krita>/kritarunner -s krita_bundled_probe -f main        (any working directory inside tests/)

Unlike the other kritarunner tests it does NOT put this repository on the module path, so the
engine it imports is the one inside the build.  Output: krita_bundled_probe.log
"""

import os
import traceback

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "krita_bundled_probe.log")


def main(*args):
    lines = []
    try:
        os.environ["KRITA_COOP_NO_REGISTER"] = "1"  # (only matters if Krita has not loaded it yet)
        import krita_coop
        from krita import Krita
        from krita_coop import protocol
        from krita_coop.kritadoc import KritaDoc

        lines.append("engine: %s" % os.path.dirname(os.path.abspath(krita_coop.__file__)))
        lines.append("protocol: %s" % protocol.PROTO_VERSION)
        d = Krita.instance().createDocument(64, 64, "probe", "RGBA", "U8", "", 72.0)
        kd = KritaDoc(d)
        lines.append("native: %s" % kd.native)  # True: this Krita has the core patch
        kd.release()
        d.close()
    except BaseException:  # noqa: BLE001
        lines.append("FAILED\n" + traceback.format_exc())
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
