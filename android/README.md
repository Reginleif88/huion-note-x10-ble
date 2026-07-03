# HiNote Sync (Android)

Native Android client for the Huion Note X10 offline-notes protocol
(see `../docs/offline-note-protocol.md`). Syncs stored pages over BLE,
renders them locally, uploads PNG + strokes JSON to your own server,
and can delete synced pages from the tablet. No Huion app, no cloud.

## Build & install

    ./gradlew :app:assembleDebug      # needs ANDROID_HOME + JDK 17
    ./gradlew :app:installDebug       # phone in USB-debugging mode

## Use

1. Settings → set your server URL (and optional auth header).
2. Tap **Sync** — every page stored on the tablet is downloaded and rendered.
3. Select pages (long-press) → **Upload** and/or **Delete on tablet**.
   Tablet deletes only work while the session is live (status "connected")
   and refuse to run if new pages appeared since the sync.

Upload format: one multipart POST per page — `page` = `page-<ts>-<n>.png`,
`strokes` = `page-<ts>-<n>.json` (same JSON schema as the Linux extractor).
Dev receiver for testing: `python3 tools/upload-receiver.py`.

## Layout

Kotlin port of the Python reference implementation in `../huion_notes/`:
`protocol/` (frames, auth, codec, sync engine — pure, JVM-tested),
`ble/` (GATT transport), `sync/` (foreground service), `store/`, `render/`,
`upload/`, `ui/` (Compose).
