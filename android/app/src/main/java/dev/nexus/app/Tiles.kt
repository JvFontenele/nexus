package dev.nexus.app

import android.app.PendingIntent
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import android.widget.Toast
import kotlin.concurrent.thread

private fun TileService.openActivity(intent: Intent) {
    intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    if (Build.VERSION.SDK_INT >= 34)
        startActivityAndCollapse(PendingIntent.getActivity(this, 0, intent, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT))
    else @Suppress("DEPRECATION") startActivityAndCollapse(intent)
}

/** Bloco rápido "Clipboard → PC". */
class ClipboardTile : TileService() {
    override fun onClick() = openActivity(Intent(this, ShareActivity::class.java).setAction(ShareActivity.ACTION_CLIPBOARD))
}

/** Bloco rápido que executa uma ação do PC escolhida no app (só ações sem parâmetros). */
class ActionTile : TileService() {
    override fun onStartListening() {
        val label = getSharedPreferences("nexus", Context.MODE_PRIVATE).getString("tile_label", null)
        qsTile?.apply {
            this.label = label ?: "Ação do PC"
            state = if (label == null) Tile.STATE_INACTIVE else Tile.STATE_ACTIVE
            updateTile()
        }
    }

    override fun onClick() {
        val action = getSharedPreferences("nexus", Context.MODE_PRIVATE).getString("tile_action", null)
            ?: return openActivity(Intent(this, MainActivity::class.java))
        val app = applicationContext
        thread {
            val msg = try {
                withPc(app) { it.runAction(action, emptyMap()) }
            } catch (e: Exception) {
                "Erro: ${e.message ?: e.javaClass.simpleName}"
            }
            Handler(Looper.getMainLooper()).post { Toast.makeText(app, "Nexus: $msg", Toast.LENGTH_SHORT).show() }
        }
    }

    companion object {
        fun choose(ctx: Context, a: RemoteAction) {
            ctx.getSharedPreferences("nexus", Context.MODE_PRIVATE).edit()
                .putString("tile_action", a.id).putString("tile_label", a.description.ifBlank { a.id }).apply()
            requestListeningState(ctx, ComponentName(ctx, ActionTile::class.java))
        }
    }
}

/** Bloco rápido "Espelhar no PC": o PC abre a tela deste celular com o scrcpy (adb por USB ou sem fio). */
class MirrorTile : TileService() {
    override fun onClick() {
        val app = applicationContext
        thread {
            val msg = try {
                val r = withPc(app) { it.request("mirror.request", org.json.JSONObject(), "action.result") }
                if (r.optBoolean("ok")) "espelhando no PC" else r.optString("error")
            } catch (e: Exception) {
                "Erro: ${e.message ?: e.javaClass.simpleName}"
            }
            Handler(Looper.getMainLooper()).post { Toast.makeText(app, "Nexus: $msg", Toast.LENGTH_LONG).show() }
        }
    }
}
