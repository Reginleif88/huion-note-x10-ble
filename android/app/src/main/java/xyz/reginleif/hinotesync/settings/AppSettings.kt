package xyz.reginleif.hinotesync.settings

import android.content.Context

class AppSettings(context: Context) {
    private val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    var serverUrl: String
        get() = prefs.getString("serverUrl", "")!!
        set(v) = prefs.edit().putString("serverUrl", v).apply()
    var headerName: String
        get() = prefs.getString("headerName", "")!!
        set(v) = prefs.edit().putString("headerName", v).apply()
    var headerValue: String
        get() = prefs.getString("headerValue", "")!!
        set(v) = prefs.edit().putString("headerValue", v).apply()
    var pin: String
        get() = prefs.getString("pin", "")!!
        set(v) = prefs.edit().putString("pin", v).apply()
    var lastMac: String
        get() = prefs.getString("lastMac", "")!!
        set(v) = prefs.edit().putString("lastMac", v).apply()
    var deleteAfterUpload: Boolean
        get() = prefs.getBoolean("deleteAfterUpload", false)   // spec: default OFF
        set(v) = prefs.edit().putBoolean("deleteAfterUpload", v).apply()
    var deleteAfterSync: Boolean
        get() = prefs.getBoolean("deleteAfterSync", false)      // destructive: default OFF
        set(v) = prefs.edit().putBoolean("deleteAfterSync", v).apply()
}
