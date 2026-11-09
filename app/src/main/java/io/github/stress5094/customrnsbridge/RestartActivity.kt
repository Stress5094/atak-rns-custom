package io.github.stress5094.customrnsbridge

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.os.Process

/**
 * Kills the bridge process and starts it again — a force-stop and relaunch,
 * without making the operator find the app in Android settings.
 *
 * WHY A SEPARATE PROCESS. Something has to still be alive to launch the new
 * activity after the old process dies, and it cannot be the dying process
 * itself. The obvious approach — schedule an AlarmManager PendingIntent, then
 * exit — was tried first and DOES NOT WORK on modern Android: an alarm firing
 * into a dead app is a background activity start, which has been blocked since
 * Android 10, so the app quit and stayed quit. This activity runs in its own
 * process (see the manifest), so it survives killing the main one, and it is
 * itself foreground at that moment, which is what makes its startActivity
 * allowed.
 *
 * Same technique as ProcessPhoenix. Deliberately does nothing else: it draws
 * no UI, holds no state, and exits immediately.
 */
class RestartActivity : Activity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // The caller's pid, passed in rather than looked up: by the time this
        // runs there is no reliable way to ask which process asked to die.
        val pid = intent.getIntExtra(EXTRA_PID, -1)
        if (pid > 0 && pid != Process.myPid()) {
            Process.killProcess(pid)
        }

        val relaunch = packageManager.getLaunchIntentForPackage(packageName)
        if (relaunch != null) {
            relaunch.addFlags(
                Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK
            )
            startActivity(relaunch)
        }

        finish()
        // This process has no other purpose, and leaving it resident would mean
        // the app shows two processes forever after one restart.
        Runtime.getRuntime().exit(0)
    }

    companion object {
        const val EXTRA_PID = "io.github.stress5094.customrnsbridge.RESTART_PID"
    }
}
