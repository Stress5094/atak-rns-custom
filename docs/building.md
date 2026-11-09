# Building it

The build itself is one command. This file is the detail behind it: which JDK,
which Python, and the two traps that have cost time before.

> **Where things live**
> [README](../README.md) · [How it works](how-it-works.md) ·
> [How it is put together](architecture.md) · [What goes on the wire](protocol.md) ·
> [Routing and airtime](routing.md) · [Running it](operations.md) ·
> [Building it](building.md) · [Settings sharing](settings-sharing.md) ·
> [Plain-language guide](mesh-explained.html) · [Range test results](../results)

```
./gradlew :app:assembleDebug
```

GitHub Actions runs the same command on every push, along with the desktop test
suite, in [.github/workflows/tests.yml](../.github/workflows/tests.yml). That
workflow is the honest test of everything on this page: it starts from a clean
machine that has none of the author's JDKs, Pythons or SDK paths, so anything
that only builds here fails there. The debug APK from each run is attached to
it as an artifact.

That produces `app/build/outputs/apk/debug/app-debug.apk`. The one thing a
fresh clone needs is `local.properties` with `sdk.dir` pointing at your Android
SDK — Android Studio writes that file for you on first open, and it is
deliberately not in the repository, being a path that is true on one machine
only.

**The FM modem library, libcodec2, isn't committed either**, and a fresh clone
needs it twice:

- **On the phone:** build the arm64 copy once, before building the APK. It lands
  in `app/src/main/jniLibs/arm64-v8a/libcodec2.so`. Without it the APK still
  builds and installs, but FM can't start.

  ```
  sdkmanager --install "cmake;4.1.2" "ndk;28.2.13676358"
  tools/build_libcodec2_android.sh
  ```

- **For the desktop tests:** install codec2 1.2.0 from your distribution
  (`libcodec2-dev` on Debian/Ubuntu, `codec2` on Arch). The modem tests need it,
  and `run_tests.sh` fails if they skip.

**Anything left in `app/src/main/python` is shipped to the phone**, because
that directory is declared as a Chaquopy source set. The desktop test venv
used to live there, which put 97 MB of desktop-built `cryptography` into every
build — silently, from the moment someone first ran `run_tests.sh`, and
invisible unless you look inside `assets/chaquopy/app.imy`. `run_tests.sh` now
builds its venv at the repo root, and `build.gradle.kts` excludes `.venv` and
`__pycache__` as a second line of defence. Fixed 2026-08-18: the debug APK went
from 55 MB to 20 MB.

One trap if you ever check this: **the incremental packager does not compact
the APK.** After the fix the file was still 55 MB with 36 MB of dead space
inside it, which reads exactly like a fix that did not work. Delete
`app/build/outputs/apk` and `app/build/intermediates/apk` and build again to
see the real size.

**Which JDK:** AGP 8.x needs JDK 17+ to *run*, not merely to target, and the
wrong one fails obscurely — a `jlink`/`JdkImageTransform` error, which is what
a stray system JDK 11 produced here once. `gradle/gradle-daemon-jvm.properties`
asks for JDK 21, so Gradle picks a matching JDK or downloads one, the same way
on any machine and whether the build is run from Android Studio or `./gradlew`.
Nothing needs setting by hand, and `org.gradle.java.home` is deliberately
absent: it takes an absolute path, which is only ever right on the machine it
was written on.

**AGP version:** AGP 8.13.2 / Gradle 8.13, after Android Studio's own
upgrade assistant bumped both together from an initial 8.2.2 / Gradle 8.5
(itself bumped once already from the original 7.4.2, to fix a JDK-21
`jlink` incompatibility). If Android Studio offers a further upgrade in the
future, be aware it edits `build.gradle.kts`/`settings.gradle.kts` and the
Gradle wrapper directly — good to know before assuming those files only
change when asked to.

**Chaquopy's build-time Python:** the `pip` that resolves `rns` and its
dependencies for Android's ABI runs on the *host*, under an interpreter that is
not the one shipped to the phone. Chaquopy 15.0.1 supports 3.8–3.12 there, and
its bundled pip still does `import cgi`, removed from the standard library in
3.13 — so on a machine whose `python3` is newer (this one is 3.14) the build
fails inside pip with an error that says nothing about versions.

`app/build.gradle.kts` therefore looks one up instead of naming a path:
`-PbuildPython=/path/to/python` wins if given, then `$CHAQUOPY_PYTHON`, then the
first usable interpreter on `PATH`, then anything installed by
[uv](https://astral.sh/uv). It prefers 3.8, because that is what Chaquopy runs
on the phone and pip only precompiles the bundled sources to `.pyc` when the two
match; any other supported version still builds, leaving that work to the phone
on first launch. If nothing suitable is found the build carries on with
Chaquopy's own default and logs what to do about it — install a 3.8, e.g. `uv
python install 3.8`, and either put it on `PATH` or pass it with `-PbuildPython`.

An earlier attempt at this exported `PYTHONPATH` from `gradlew` to shim in just
the one function pip needed. That worked for command-line builds but silently
did nothing for Android Studio, which drives Gradle through the Tooling API
rather than executing the `gradlew` script — which is why the fix lives in
`build.gradle.kts` and not in a wrapper script.
