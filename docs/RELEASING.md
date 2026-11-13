# Releasing

Pushing a version tag builds a signed APK and publishes it as a GitHub
Release:

```bash
git tag v1.1
git push origin v1.1
```

That runs the full test suite first. A tag whose suite fails publishes
nothing.

Ordinary pushes are unchanged: they run the suite and keep a debug APK as a
workflow artifact for 90 days. Only a tag produces a release.

## One-time setup: the signing key

Android will not install an unsigned APK, and an app can only be upgraded by
a build signed with the **same** key it was first installed with. So this key
has to be created once, kept for the life of the app, and never lost.

### 1. Create the keystore

Run this on your own machine, not in CI. It asks for a password, then for
name and organisation details, which are not checked by anything and can be
whatever you like — they end up in the certificate, so a project name is
fine and a home address is not.

```bash
keytool -genkeypair -v \
  -keystore rtak-release.jks \
  -alias rtak \
  -keyalg RSA -keysize 4096 \
  -validity 10000 \
  -storetype PKCS12 \
  -dname "CN=rtak"
```

`-dname` sets the certificate's name outright and skips the questions. The
name is free text and is not checked by anything — use whatever you want this
project's certificate to say. It is worth passing explicitly: answering those
prompts interactively and pressing return through them produces a certificate
reading `CN=…, OU=Unknown, O=Unknown, L=Unknown, ST=Unknown, C=Unknown`, and
that name is embedded in every APK you publish for anyone to read.

**The name cannot be changed later.** Android identifies an app by its signing
certificate, so re-issuing the certificate under a different name is as
breaking as a new key: nobody who installed an earlier release could be
upgraded. Pick it before the first release and keep it.

`-validity 10000` is about 27 years. A key that expires is a key that cannot
ship an upgrade, so this is deliberately long.

### 2. Back it up before you do anything else

**If you lose this file or its password, you can never update the app for
anyone who already installed it.** They would have to uninstall and lose their
settings. There is no recovery path: Android identifies an app by its signing
key.

Keep a copy somewhere that is not this machine and not this repository. The
file is gitignored here precisely so it cannot be committed by accident.

### 3. Add four secrets to GitHub

*Settings → Secrets and variables → Actions → New repository secret.*

| Secret | Value |
|---|---|
| `RELEASE_KEYSTORE_BASE64` | the keystore file, base64 encoded (below) |
| `RELEASE_KEYSTORE_PASSWORD` | the keystore password |
| `RELEASE_KEY_ALIAS` | `rtak`, or whatever you passed to `-alias` |
| `RELEASE_KEY_PASSWORD` | **the same password again** — see below |

`RELEASE_KEY_PASSWORD` and `RELEASE_KEYSTORE_PASSWORD` hold the same value.
A PKCS12 keystore cannot have a separate key password; `keytool` says so and
ignores any `-keypass` you give it:

```
Warning: Different store and key passwords not supported for PKCS12
KeyStores. Ignoring user-specified -keypass value.
```

Gradle still asks for both, so both secrets have to exist.

To produce the base64, as one line with no wrapping:

```bash
base64 -w0 rtak-release.jks
```

On macOS, `base64 -i rtak-release.jks | tr -d '\n'`.

Paste the whole string. GitHub masks secrets in logs, and the workflow writes
the keystore to the runner's temporary directory, uses it for one build, and
deletes it.

## How the version number is set

The tag is the version. `v1.2.3` builds `versionName = 1.2.3` and
`versionCode = 10203`, from `major * 10000 + minor * 100 + patch`.

- `versionCode` must only ever increase, or a phone will not accept a new
  build as an upgrade. This scheme keeps that true as long as minor and patch
  stay below 100, and the workflow refuses a tag it cannot convert rather than
  publishing a version that sorts below its predecessor.
- A tag that is not `vMAJOR.MINOR` or `vMAJOR.MINOR.PATCH` fails the build on
  purpose.
- Local builds are unaffected and keep reading `1.0`. The build stamp on the
  status screen, not the version name, is what tells two local builds apart.

## What a release contains

One APK, `arm64-v8a` only, for the phone. No mapping file, no bundle, no store
listing.

The debug APK that every push produces is **not** a release. It is a
debuggable build, and in a debuggable build this app registers a broadcast
receiver that lets any other app on the phone key the radio. That is a bench
convenience and the reason debug builds should stay on your own phones.

## If the build says the APK is unsigned

The workflow checks, and fails loudly, because Gradle will otherwise produce
`app-release-unsigned.apk` and report success. It means at least one of the
four secrets did not reach Gradle — usually a name typo, or base64 that was
pasted with line breaks in it.
