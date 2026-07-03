package xyz.reginleif.hinotesync.render

import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import xyz.reginleif.hinotesync.protocol.PageData

object PageRenderer {
    fun render(page: PageData, width: Int = 900, height: Int = 1190, pad: Float = 15f): Bitmap {
        val bmp = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bmp)
        canvas.drawColor(Color.WHITE)
        val paint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
            color = Color.rgb(17, 17, 17)
            style = Paint.Style.STROKE
            strokeCap = Paint.Cap.ROUND
            strokeJoin = Paint.Join.ROUND
        }
        fun sx(x: Int) = pad + x / page.limits.maxX * (width - 2 * pad)
        fun sy(y: Int) = pad + y / page.limits.maxY * (height - 2 * pad)
        for (stroke in page.strokes) {
            for (i in 1 until stroke.size) {
                val p = stroke[i]
                paint.strokeWidth = 1.5f + 2.5f * (p.press / page.limits.maxPress)
                canvas.drawLine(sx(stroke[i - 1].x), sy(stroke[i - 1].y), sx(p.x), sy(p.y), paint)
            }
        }
        return bmp
    }
}
