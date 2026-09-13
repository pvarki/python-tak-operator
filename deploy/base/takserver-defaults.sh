#!/usr/bin/env sh
# TAK's setenv.sh sources this after forcing TLS 1.2. Prefer TLS 1.3 for CNPG,
# retaining TLS 1.2 for existing TAK peers and all upstream JVM module options.
export JDK_JAVA_OPTIONS="${JDK_JAVA_OPTIONS:-} -Djdk.tls.client.protocols=TLSv1.3,TLSv1.2"
