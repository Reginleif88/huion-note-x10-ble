package xyz.reginleif.hinotesync.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class AuthTest {
    @Test fun verifyResponseMatchesCapturedVectors() {
        // sync-01: challenge (22,122,69) -> (0x42,0xfe,0x3d)
        assertEquals(Triple(0x42, 0xFE, 0x3D), verifyResponse(22, 122, 69))
        // sync-multipage session 2: (28,63,239) -> (0x6d,0xbc,0xe7)
        assertEquals(Triple(0x6D, 0xBC, 0xE7), verifyResponse(28, 63, 239))
    }

    @Test fun buildVerifyResultMatchesCapture() {
        assertEquals("cd820842fe3d00ed", buildVerifyResult(22, 122, 69).hex())
        assertEquals("cd82086dbce700ed", buildVerifyResult(28, 63, 239).hex())
    }

    @Test fun encodePwdAppliesHuionOffsets() {
        assertArrayEquals(intArrayOf(153, 167, 156, 163, 163, 89), encodePwd("123456"))
    }

    @Test fun encodePwdRejectsBadPins() {
        for (bad in listOf("12345", "1234567", "12345a", "")) {
            assertThrows(IllegalArgumentException::class.java) { encodePwd(bad) }
        }
    }

    @Test fun verifyPwdFramesTwoFrameLayout() {
        val (f1, f2) = buildVerifyPwdFrames("123456")
        assertEquals("cd83080199a79ced", f1.hex())
        assertEquals("cd830802a3a359ed", f2.hex())
    }
}
