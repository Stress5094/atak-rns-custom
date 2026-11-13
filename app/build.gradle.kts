import java.io.ByteArrayOutputStream
import java.io.File

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

/** Short commit, plus a "+" when the tree has uncommitted changes. */
fun gitStamp(): String = try {
    val out = ByteArrayOutputStream()
    exec {
        commandLine("git", "describe", "--always", "--dirty=+", "--abbrev=7")
        standardOutput = out
        errorOutput = ByteArrayOutputStream()
    }
    out.toString().trim().ifEmpty { "unknown" }
} catch (e: Exception) {
    // Never fail a build over a cosmetic label -- a source drop without a .git
    // directory should still compile.
    "unknown"
}

/**
 * The interpreter Chaquopy runs `pip` under while building.
 *
 * This is the *host* Python, not the one that ships on the phone. Chaquopy
 * 15 supports 3.8-3.12 (this app needs 3.9+, see onPath below), and its
 * bundled pip still imports `cgi`, which was removed from the standard library
 * in 3.13 -- so on a machine whose default python3 is newer, the build fails
 * inside pip with a confusing error unless an older interpreter is named
 * explicitly.
 *
 * Order of preference: `-PbuildPython=/path/to/python`, then $CHAQUOPY_PYTHON,
 * then the first usable interpreter on PATH, then anything installed by `uv`.
 * Returning null leaves Chaquopy to its own default, which is right when the
 * machine's own python3 is already old enough.
 */
fun isUsablePython(command: String): Boolean = try {
    ProcessBuilder(command, "-c",
        "import sys; sys.exit(0 if (3, 9) <= sys.version_info[:2] <= (3, 12) else 1)")
        .redirectErrorStream(true)
        .start()
        .waitFor() == 0
} catch (e: Exception) {
    // Not installed, not executable: just means "not this one".
    false
}

fun buildPythonCommand(): String? {
    val named = (findProperty("buildPython") as String?) ?: System.getenv("CHAQUOPY_PYTHON")
    // An explicitly named interpreter is used as given, even if it looks
    // wrong -- being overruled silently is worse than a clear pip failure.
    if (!named.isNullOrBlank()) return named

    // 3.9 first because that is the Python this app ships on the phone (see
    // `version` in the chaquopy block below), and pip only precompiles the
    // bundled sources to .pyc when the build-time interpreter matches the
    // on-device one. Any other usable version still builds -- it just leaves
    // the compiling to the phone, on first launch.
    //
    // 3.8 IS NOT ON THIS LIST ANY MORE, and not only because the phone moved:
    // Chaquopy checks a package's Requires-Python against THIS interpreter, not
    // the phone's. `meshtastic` needs 3.9+, and under a 3.8 host pip stops with
    // "requires a different Python: 3.8.20 not in '>=3.9'" -- found the hard way
    // on a machine that had both 3.8 and 3.11 installed.
    val onPath = listOf("python3.9", "python3.10", "python3.11",
                        "python3.12", "python3", "python")
    onPath.firstOrNull { isUsablePython(it) }?.let { return it }

    // `uv python install 3.11` puts interpreters here, off PATH.
    val uvRoot = File(System.getProperty("user.home"), ".local/share/uv/python")
    uvRoot.listFiles()?.sortedByDescending { it.name }?.forEach { dir ->
        val candidate = File(dir, "bin/python3")
        if (candidate.canExecute() && isUsablePython(candidate.absolutePath)) {
            return candidate.absolutePath
        }
    }

    logger.warn("chaquopy: no Python 3.9-3.12 found; leaving buildPython unset. " +
                "If the build fails inside pip, install one and re-run with " +
                "-PbuildPython=/path/to/python3.11")
    return null
}

android {
    namespace = "io.github.stress5094.customrnsbridge"
    compileSdk = 34

    defaultConfig {
        applicationId = "io.github.stress5094.customrnsbridge"
        minSdk = 26
        targetSdk = 33
        // Overridden by the release workflow from the git tag, so a published
        // build carries the version it was released as. Unset everywhere else,
        // which keeps a local build reading 1.0 exactly as it always has --
        // the BUILD_STAMP below is what tells two local builds apart.
        versionCode = (System.getenv("RTAK_VERSION_CODE") ?: "1").toInt()
        versionName = System.getenv("RTAK_VERSION_NAME") ?: "1.0"

        ndk {
            abiFilters.add("arm64-v8a")
        }

        // A stamp that actually changes between builds. versionName has read
        // "1.0" since the first commit, so two phones showing it prove nothing
        // -- and "did both phones get the same build?" is the question the
        // status screen has to be able to answer, because a half-finished
        // rollout gives two radios that transmit perfectly and ignore each
        // other.
        buildConfigField("String", "BUILD_STAMP", "\"${gitStamp()}\"")
    }

    buildFeatures {
        buildConfig = true
    }

    // Release signing, supplied by the environment and never by a file in the
    // repository. The release workflow writes the keystore out of a GitHub
    // secret, builds, and deletes it again; nothing here reads a path that a
    // clone would have.
    //
    // All four variables have to be present or the config is not created at
    // all: a half-configured signingConfig fails the build with a message
    // about a null password, which reads like a Gradle bug rather than a
    // missing secret. Absent, `assembleRelease` keeps its old behaviour and
    // produces an UNSIGNED apk, which Android will refuse to install -- that
    // is the honest outcome for a build with no key, not a problem to hide.
    val keystorePath = System.getenv("RTAK_KEYSTORE_FILE")
    val keystorePassword = System.getenv("RTAK_KEYSTORE_PASSWORD")
    val keystoreAlias = System.getenv("RTAK_KEY_ALIAS")
    val keystoreKeyPassword = System.getenv("RTAK_KEY_PASSWORD")
    val signingReady = !keystorePath.isNullOrBlank() &&
            !keystorePassword.isNullOrBlank() &&
            !keystoreAlias.isNullOrBlank() &&
            !keystoreKeyPassword.isNullOrBlank()

    if (signingReady) {
        signingConfigs {
            create("release") {
                storeFile = file(keystorePath!!)
                storePassword = keystorePassword
                keyAlias = keystoreAlias
                keyPassword = keystoreKeyPassword
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (signingReady) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }

    kotlinOptions {
        jvmTarget = "11"
    }
}

chaquopy {
    defaultConfig {
        // Host-machine Python used to run pip during the build (not the
        // runtime Python shipped on-device). Found rather than hardcoded --
        // see buildPythonCommand() above. Null means "whatever Chaquopy
        // picks", which is correct on a machine whose python3 is <= 3.12.
        buildPythonCommand()?.let { buildPython(it) }
        // 3.9, NOT Chaquopy 15's default of 3.8, because the official
        // `meshtastic` package (the Meshtastic link, mt_link.py) needs 3.9+.
        //
        // WHY 3.9 AND NOT NEWER. Chaquopy's repository builds `cryptography`
        // 3.4.8 for 3.8, 3.9 and 3.10 but jumps to 42.0.8 from 3.10 up. That
        // library does the mesh's AES-CCM, so 3.9 is the one step that meets
        // the package's minimum and keeps the encryption library on the exact
        // version this app has always shipped. Going to 3.11 would change it
        // along with the interpreter, and a failure in either would look like
        // the other.
        version = "3.9"
        pip {
            // PINNED 2026-11-07. Unpinned, a rebuild picked up rns 1.6.0, whose
            // Transport.add_interface() reads interface.announce_rate_target --
            // an attribute only Reticulum's own config loader sets, and which
            // the hand-built FM interface lacks -- so FM failed to come up
            // ("FM backup interface could not be built"). 1.5.x does not read
            // it there. 1.5.7 is the newest release found on this machine from
            // before the break; the exact version the Oct 31 build shipped was
            // never recorded. Upgrade deliberately, on the bench.
            install("rns==1.5.7")
            // The whole of meshtastic's own requirements are installed, even
            // though the serial path imports only protobuf, pypubsub,
            // tabulate, requests and pyserial. `bleak` (Bluetooth, loaded only
            // by ble_interface.py) and `pyyaml` (only its command-line
            // __main__) ride along because Chaquopy's install() takes one
            // requirement and no pip flags, so there is no --no-deps. The
            // version is pinned: the client talks to firmware, and an
            // unpinned upgrade is an unmeasured change to the protocol layer.
            install("meshtastic==2.7.11")
        }
    }
    sourceSets {
        getByName("main") {
            srcDirs("src/main/python")

            // Everything in that directory is packaged into the APK and
            // shipped to the phone. A test venv there put 97 MB of
            // desktop-built `cryptography` into every build -- silently, from
            // the moment someone first ran run_tests.sh, and invisible unless
            // you go looking inside assets/chaquopy/app.imy.
            //
            // run_tests.sh now creates its venv at the repo root instead, so
            // this is the second line of defence: an IDE that helpfully makes
            // one here cannot put it back. __pycache__ is desktop bytecode for
            // a different Python than the phone runs.
            //
            // The test suite is excluded for a different reason: it cannot run
            // on the phone and nothing on the phone imports it, but every file
            // in this directory is packaged, so the APK was carrying 1.4 MB of
            // tests against 1.6 MB of code that actually runs -- about half the
            // Python payload. The files stay in the repository, where they are
            // the thing that makes a release trustworthy; they just have no
            // business on a handset.
            //
            // rns_stub is the RNS/pyserial double the suite imports instead of
            // the real thing, and train_zdict regenerates compact_zdict.py on a
            // desktop. Both are named in comments in runtime modules, which is
            // not an import -- checked before excluding them.
            exclude(
                ".venv/**", "**/__pycache__/**",
                "test_*.py", "run_tests.sh", "rns_stub.py", "train_zdict.py",
            )
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.10.1")
    implementation("androidx.appcompat:appcompat:1.6.1")
    // Material 3 (operator, 2026-10-22): the screens follow Google's Material
    // guidelines with Google's own components -- bottom navigation, button
    // emphasis, dialogs, switches, a light AND dark theme -- rather than a
    // hand-made look-alike. The one dependency added on purpose since this
    // list was kept to three. 1.12 is the newest that builds at compileSdk 34.
    implementation("com.google.android.material:material:1.12.0")
    implementation("com.github.mik3y:usb-serial-for-android:3.7.3")
    // Settings sharing by QR code (the settings-sharing notes): showing one
    // (zxing core) and scanning one with the camera. Fully offline, unlike
    // Google's code scanner, which needs Play services and may download its
    // model on first use -- in the field, with no signal.
    implementation("com.journeyapps:zxing-android-embedded:4.3.0")
    // JVM-only tests for code with no Android imports (MeshtasticBleFraming).
    testImplementation("junit:junit:4.13.2")
}
