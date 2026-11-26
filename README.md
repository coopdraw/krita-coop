# Krita Coop: Krita with real-time co-op drawing

A custom build of Krita 5.3.4.1 in which two or more people work on the same canvas over the network, similar to Drawpile. One person hosts their open document; the others join with an address and a session code. Every layer is shared, and people can draw on the same layer at the same time.

This is an unofficial, experimental modification. It is not made, endorsed or supported by the Krita project or KDE; please don't report its problems to them. Krita itself is at <https://krita.org>.

## What it is made of

The co-op build is standard Krita plus two things from this repository. Neither works without the other.

| Part | Where | What it does |
|---|---|---|
| Core patch | `krita-patches/coop-core.patch` (C++, about 470 lines) | Lets code inside Krita see a stroke before it ends, hold a running stroke for a moment, write pixels with a partial repaint, and learn which parts of which layers changed. |
| Co-op engine | `krita_coop/` (Python) | Everything else: networking, the host's master copy, merging everyone's pixels, per-person undo, the docker. |

The engine is built into the co-op build as one of Krita's own Python plugins and is switched on by default. There is nothing to install separately: start the co-op build and open **Settings > Dockers > Coop Drawing**.

## Get it

* **Windows:** build it with the scripts in `krita-build\windows\`: [krita-build/README.md](krita-build/README.md). With the default build tree the result is `D:\krita-dev\pkg\krita-coop-x64-5.3.4.1\bin\krita.exe` (a self-contained folder; `krita.lnk` in it does the same), and the same as a zip for another Windows PC.
* **macOS:** built by a GitHub workflow (Actions > macOS build), or on your own Mac with `krita-build/mac/build.sh`: [krita-build/MAC.md](krita-build/MAC.md). The Apple Silicon build passes the automatic tests on GitHub's Macs; it has not been used on a real Mac yet.

The co-op build uses the same settings and brushes as a normal Krita 5.3 on the same computer. Everyone in a session needs the same version of the co-op build: a partner whose version speaks another protocol is refused.

If an older, separately installed "Coop Drawing" plugin is still in Krita's resource folder (`pykrita/krita_coop` and `pykrita/krita_coop.desktop`), delete it. The co-op build uses its own built-in copy, but a standard Krita would still load the old one.

## What sharing looks like

* **Others see your stroke while you paint it**, including brushes in *Wash* mode (most of Krita's brushes).
* **You see what others paint while you are mid-stroke.** Your stroke is held between two of its steps for a few milliseconds while their pixels are written.
* **The canvas repaints only the changed area**, without waiting.

Measured with real brush strokes inside the co-op build (`tests/run_live_gui.ps1`, 2000×1600, host and guest in one Krita): the guest saw a hand-drawn stroke 0.1 s after it began, the host received the guest's paint about 0.1 s after it was drawn while its own pen was still down, and holding a stroke to write 13 tiles took about 1 ms (3 ms at the 95th percentile).

What waits until you finish what you are doing:

* **Tiles under your own Wash stroke.** While your stroke is in progress, other people's paint in the 64×64 tiles it has touched appears when you lift the pen.
* **Layers under an open tool action** (Transform, Move, a filter dialog): changes to those layers appear when the action is finished. Other layers keep updating.
* **Layer structure** (new, deleted, moved, renamed layers) is applied between strokes.
* **Instant Preview** is switched off for the session and restored afterwards.

## Use

* **Host:** open a document, then press **Start hosting**, then **Copy invite**. Send the address and code.
* **Join:** open the **Join** tab, paste the address and code, and press **Join**. A new document opens with the host's layers.
* **Draw:** draw on any layer. Anyone can paint, erase, transform, lock, rename, reorder, group or delete any layer.
* **Undo:** Ctrl+Z (Cmd+Z on a Mac), Edit > Undo and the toolbar button undo **your own** last change in the shared document. Paint someone else put on top of it stays. Redo works the same way. The docker also has **Undo mine** and **Redo mine** buttons, plus a count of your undo steps. See [Undo during a session](#undo-during-a-session).
* **Chat and cursors:** the docker has a chat box, and each person's cursor is shown on the others' canvas. The cursor sharing is experimental and can be turned off.

### Networking

* **Same network (LAN or Wi-Fi):** works directly. Windows asks once whether Krita may accept connections; allow it for private networks.
* **Over the internet:** the simplest option is a mesh VPN such as Tailscale or ZeroTier. Use the host's VPN address. The other option is to forward TCP port 27760 on the host's router.
* **Security:** the session code keeps strangers out, but traffic is **not encrypted**.

## How it works

Krita's brush engines don't produce bit-identical results on different computers, so strokes can't be replayed on the other side as Drawpile does. The engine syncs **pixels** instead.

* **The host keeps a master copy.** It runs a small server inside its Krita, holding the authoritative layer tree and every 64×64 tile in compressed form. The host's own document is just another client of that server. Because the server is plain data, it merges and relays guests' changes immediately, even while the host is mid-stroke.
* **Clients send only the pixels they changed.** Krita reports which parts of which layers were just changed (core patch). Each client re-reads those under a small per-frame time budget, including a Wash stroke still in progress, compares each tile with what it expects the tile to contain, and sends a patch of the changed pixels plus a per-pixel mask. A slow re-read of everything runs as a safety net.
* **The host merges patches pixel by pixel, in arrival order,** and broadcasts the merged tiles.
* **Clients merge the result back in.** When a merged tile comes back, a client rebuilds it as the host's content plus its own unconfirmed and unsent edits. Two people painting the same area of one layer at once therefore both keep their strokes. Only pixels both changed go to whichever change arrived last.
* **Layer structure follows the same pattern.** Clients send create, delete, move, regroup and property operations; the server applies them and broadcasts the tree.
* **Others' pixels are written while you draw.** If a stroke is running, it is held between two of its jobs for a few milliseconds (core patch) while the tiles are merged and written, so no dab is lost; the canvas then repaints just that area. Layer structure from others is applied only when no stroke is in progress. Both use calls that bypass Krita's command system, so they never appear as entries in your undo history. The exceptions are blending-mode changes and the canvas size, which only exist as Krita commands. Remote changes do mark the document as modified, so Krita offers to save what others drew.
* **Undo is per person and done by the engine.** Krita can't undo correctly on a shared layer: each layer keeps one list of changes, and Ctrl+Z takes back the newest one, whoever made it. So during a session:
  * Each pixel patch you send is tagged with your current *action* (a stroke, fill, transform...). An action is closed once Krita is idle again and the layers you touched have been re-read completely. A stroke started before that joins the same action.
  * The host keeps the history, as in Drawpile. For every tile it keeps a starting state plus everyone's actions in the order they arrived. Undo switches one of your actions off, and the host recomputes the affected tiles from the starting state and every action still on. Redo switches it back on.
  * The result doesn't depend on who undoes first. If everyone undoes everything, the canvas returns to how it started. Where your partner painted over your stroke, their paint stays on top after you undo.
  * The engine takes over Krita's Undo/Redo actions for the shared document only. Other open documents keep Krita's normal undo.

The engine's time on Krita's UI thread during a hand-drawn stroke was about 6 ms per 16 ms frame (13 ms at the 95th percentile) in `tests/run_live_gui.ps1`. Most of it is the pixel comparison, which is still done in Python.

## Limitations

* **Undo during a session covers your pixel changes only** (see [Undo during a session](#undo-during-a-session)).
* **Overlapping pixels go to one person.** If both people paint the very same pixels at the same moment, the change that reaches the host last wins for those pixels; the two strokes are not blended there.
* **Only paint layers and groups are shared.** Vector, fill, filter, file and clone layers stay local, as do masks, layer styles, selections, guides and animation (only the current frame is shared). Layers must use the document's colour space.
* **The host controls the canvas size.** A resize on the host makes it republish every layer.

## Undo during a session

* **What it undoes:** your own last action on shared paint layers (strokes, fills, erasing, transforms, filters, Clear...), for everyone. Paint someone else put on top of it stays. If someone else's paint covers your change completely, Ctrl+Z skips it, says so, and undoes your change before it.
* **Overlapping soft edges:** the engine syncs pixels, not brush strokes. Where a soft brush edge was painted over someone else's paint, that edge's pixels already contain the other color. If the other person undoes their paint, a faint tint can stay along your edge until you undo yours as well.
* **What it can't undo:** layer changes (add, delete, move, rename, opacity, blending mode, lock...), selections, and edits on layers that aren't shared (vector layers, masks...). For these, Ctrl+Z stops with a message, and pressing it again undoes your painting before them. An action that also deleted layers, such as Merge Down, can't be undone during a session. **Deleting a shared layer is final for everyone**, so be careful with Delete Layer during a session.
* **Not undoable at all:** what was on the canvas when the session started, a layer's first content (new, duplicated or pasted layers), and changes made while a layer was still being shared at the start.
* **Pressing Ctrl+Z while a tool is open:** with the Transform or Move tool, or a filter dialog, still open, Ctrl+Z is skipped with a hint. Finish or cancel the tool first. If you press Ctrl+Z while a stroke is still finishing, it waits briefly. If you start drawing again first, the undo is cancelled.
* **Quick strokes:** a stroke started a split second after the previous one may count as one action with it.
* **Limits:** 100 steps per person. The host keeps at most about 384 MB of history; past that, the oldest steps can no longer be undone ("too old"). Leaving the session ends your undo history. A canvas resize clears everyone's history, and switching animation frames clears yours.
* **Undo History docker:** greyed out for the shared document.
* **After the session:** Krita's own undo history for that document still mixes everyone's changes. Undo stays off for steps from the session; steps you make afterwards undo normally. If you press undo past them, Krita asks whether to use its own undo anyway. Save and reopen the document to get a clean history.
* **Crop tool:** after the host crops during a session, don't reopen the Crop tool to adjust that crop. Krita would undo the crop through its own history.

## Layout

```
krita_coop/               the co-op engine; the build copies it into Krita as a built-in Python plugin
  kritapykrita_krita_coop.desktop   plugin descriptor
  __init__.py             registers the docker
  docker.py, controller.py, cursors.py   UI (PyQt)
  undokeys.py             routes Krita's Undo/Redo actions to the session (shared document only)
  undo.py                 per-person undo: recording, action boundaries, undo/redo jobs
  kritadoc.py             adapter over Krita's Document/Node API, including the core patch's coop* calls
  session.py              Client (one per document), HostSession, GuestSession
  server.py               authoritative tree + tiles, pixel merging (host only)
  replica.py              client tile state, change scanner, merge-back
  pixops.py               fast per-pixel diff/select with big integers (no numpy)
  tree.py                 layer-tree model, diff -> ops, op application
  protocol.py, net.py     wire format, TCP transport, handshake
tests/
  test_engine.py          host + guests over localhost on fake documents
  test_undo.py            per-person undo scenarios, wire format, fuzzing
  test_pixops.py          pixel maths against a naive reference
  krita_integration.py    the same flows against the real Krita API (kritarunner)
  krita_ui_smoke.py       docker/controller smoke test (kritarunner)
  krita_perf.py           latency / UI-thread cost (kritarunner)
  test_native.py          live mode on fake documents that mimic the co-op build
  krita_live_gui.py       real brush strokes inside the co-op build (started by run_live_gui.ps1)
krita-patches/
  coop-core.patch         the changes to Krita 5.3.4.1 (git tag v6.0.4.1)
  krita_bundled_probe.py  is the engine really built into this Krita? (kritarunner, no repository on the path)
krita-build/
  README.md               building on Windows, what the patch adds
  MAC.md                  building on macOS
  windows/                build, package and test scripts
  mac/build.sh            the macOS build, stage by stage
.github/workflows/macos.yml   runs mac/build.sh on GitHub and offers the app as a download
```

The engine still contains the code paths for a Krita without the core patch (remote changes then wait until you lift the pen). The tests on fake documents use them, and they are what a standard Krita would run if the engine were installed there as a plugin. That is no longer a supported way to use it.

Run the tests (Windows; details in [krita-build/README.md](krita-build/README.md)):

```powershell
python -m unittest discover -s tests                              # engine, on fake documents
krita-build\windows\kritarunner-test.bat krita_integration        # real Krita API, no window; writes tests\krita_integration.log
powershell -ExecutionPolicy Bypass -File tests\run_live_gui.ps1   # real brush strokes; opens Krita for a minute
```

## Licence

Krita is free software under the GNU General Public License, and the core patch here is a modification of Krita's source. Everything in this repository is made available under the GNU General Public License, version 3 or (at your option) any later version; see [LICENSE](LICENSE). It comes with no warranty.

"Krita" is a trademark of the Stichting Krita Foundation. This repository is not affiliated with it.
