package dev.nexus.app

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.math.BigInteger
import java.net.Socket
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.Principal
import java.security.PrivateKey
import java.security.SecureRandom
import java.security.cert.X509Certificate
import java.security.spec.ECGenParameterSpec
import java.util.Base64
import java.util.Date
import java.util.UUID
import javax.net.ssl.X509KeyManager
import javax.security.auth.x500.X500Principal

/** Identidade do aparelho: EC P-256 no AndroidKeyStore (chave não exportável) + certificado autoassinado do próprio KeyStore. */
class Identity private constructor(val key: PrivateKey, val cert: X509Certificate) {
    val deviceId: String = sha256Hex(cert.encoded)
    val certPem: String = "-----BEGIN CERTIFICATE-----\n" +
        Base64.getMimeEncoder(64, "\n".toByteArray()).encodeToString(cert.encoded) +
        "\n-----END CERTIFICATE-----\n"

    val keyManager = object : X509KeyManager {
        override fun chooseClientAlias(keyType: Array<String>?, issuers: Array<Principal>?, socket: Socket?) = ALIAS
        override fun getCertificateChain(alias: String?) = arrayOf(cert)
        override fun getPrivateKey(alias: String?) = key
        override fun getClientAliases(keyType: String?, issuers: Array<Principal>?) = arrayOf(ALIAS)
        override fun chooseServerAlias(keyType: String?, issuers: Array<Principal>?, socket: Socket?): String? = null
        override fun getServerAliases(keyType: String?, issuers: Array<Principal>?): Array<String>? = null
    }

    companion object {
        private const val ALIAS = "nexus-id"
        private const val DAY = 86_400_000L

        fun load(): Identity {
            val ks = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
            if (!ks.containsAlias(ALIAS)) {
                val now = System.currentTimeMillis()
                val gen = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, "AndroidKeyStore")
                gen.initialize(
                    KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN)
                        .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                        .setDigests(KeyProperties.DIGEST_SHA256, KeyProperties.DIGEST_NONE)
                        // CN único: o cert do KeyStore não traz SKID/AKID e o OpenSSL casa âncoras pelo subject
                        .setCertificateSubject(X500Principal("CN=nexus-${UUID.randomUUID()}"))
                        .setCertificateSerialNumber(BigInteger(63, SecureRandom()))
                        // um dia para trás tolera relógio do PC atrasado
                        .setCertificateNotBefore(Date(now - DAY))
                        .setCertificateNotAfter(Date(now + 3650 * DAY))
                        .build()
                )
                gen.generateKeyPair()
            }
            val cert = ks.getCertificate(ALIAS) as X509Certificate
            cert.verify(cert.publicKey) // #007: o certificado precisa ser assinado de verdade pela própria chave
            return Identity(ks.getKey(ALIAS, null) as PrivateKey, cert)
        }
    }
}
