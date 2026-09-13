# TAK database with CloudNativePG

`task tak:up` installs the pinned CNPG 1.30.0 operator, creates the local database
Cluster, waits for its `Ready` condition, then deploys TAK. `task database:up`
performs only the database steps; `task cnpg:up` installs only the operator.
All tasks target the shared `kind-rmk8soperator` development cluster.

The bundles are separate so an existing production CNPG installation can manage
the database without installing another operator:

- `deploy/cnpg`: upstream operator release, installed with server-side apply.
- `deploy/database/base`: three-instance PostgreSQL 15 Cluster with 10 GiB per
  instance. Choose storage, resources, placement and backups in a deployment
  overlay before using this in production; three instances alone are not a
  complete production configuration.
- `deploy/database/local`: one instance with a 2 GiB PVC for the shared kind VM.
- `deploy/database/migrate-legacy`: one-time logical import of the former local
  standalone database into a new CNPG Cluster.

The PostGIS image is pinned by digest and supports AMD64 and ARM64. CNPG creates
`cot`, owned by the non-superuser `martiuser`, and installs `postgis` and
`pgcrypto` before TAK's SchemaManager initializes its tables. Remote superuser
access is disabled. TAK connects through CNPG's primary service `tak-database-rw`
using the password in the `kubernetes.io/basic-auth` Secret `tak-database-app`.
CNPG owns the database Pods, Services and PVCs.

CNPG retains its TLS 1.3 minimum. TAK's upstream `setenv.sh` forces outbound TLS
1.2, so a ConfigMap mounted at `/etc/default/takserver` appends a JVM option that
enables TLS 1.3 first while retaining TLS 1.2 for other TAK peers. This uses TAK's
existing defaults hook and preserves its JVM module options. It applies to all
three TAK services; SchemaManager already uses the JVM's TLS 1.3 capable defaults.

`ensure-secrets.sh` generates credentials only when absent. For an older checkout,
it copies the existing `tak-secrets.POSTGRES_PASSWORD` into `tak-database-app`.
TAK reads the latter explicitly; `tak-secrets` holds its certificate passwords.
Preserve both Secrets and the database and TAK PVCs across upgrades. Password
rotation requires coordinating CNPG's role password and a TAK restart; this
startup helper does not rotate passwords.

The database ingress policy permits TAK and peer database Pods on TCP 5432,
and the CNPG operator in `cnpg-system` on TCP 5432 and 8000. Adapt the operator
namespace and labels if its installation differs. The default kind CNI does not
enforce NetworkPolicy.

Without Task, install `deploy/cnpg` with `kubectl apply --server-side -k`, wait for
`clusters.postgresql.cnpg.io` to be Established and `cnpg-controller-manager` in
`cnpg-system` to roll out, then run `ensure-secrets.sh`, apply
`deploy/database/local`, and wait for Cluster `tak-database` to be Ready before
applying `deploy/overlays/local`.

## Migrating the previous local database

`database:up` refuses to create an empty replacement while Deployment
`tak-database` exists. TAK's persistent initialization marker would otherwise
skip schema creation, and the new database would lack the old data.

This import is for the old local deployment only, before a CNPG Cluster named
`tak-database` exists. CNPG bootstrap imports run once. Do not apply the import
overlay to an already initialized Cluster expecting it to replace its contents.

Install the operator and preserve credentials first:

```sh
task cnpg:up
bash deploy/scripts/ensure-secrets.sh
kubectl --context kind-rmk8soperator -n tak-operator-system \
  get clusters.postgresql.cnpg.io/tak-database --ignore-not-found
```

The last command must return no Cluster. Stop writers while leaving the old
database available for the import:

```sh
kubectl --context kind-rmk8soperator -n tak-operator-system \
  scale deployment/tak-operator deployment/takserver --replicas=0
kubectl --context kind-rmk8soperator -n tak-operator-system \
  wait --for=delete pod -l app.kubernetes.io/name=takoperator --timeout=120s
kubectl --context kind-rmk8soperator -n tak-operator-system \
  wait --for=delete pod -l app.kubernetes.io/name=takserver --timeout=120s
kubectl --context kind-rmk8soperator apply -k deploy/database/migrate-legacy
kubectl --context kind-rmk8soperator -n tak-operator-system \
  wait --for=condition=Ready cluster.postgresql.cnpg.io/tak-database --timeout=600s
```

Verify the imported schema and data before removing the old workload. For example,
compare public table counts on the old database and the new primary, then inspect
any application records you expect to retain:

```sh
kubectl --context kind-rmk8soperator -n tak-operator-system \
  exec deployment/tak-database -- psql -U martiuser -d cot -c \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
primary=$(kubectl --context kind-rmk8soperator -n tak-operator-system \
  get cluster.postgresql.cnpg.io/tak-database -o jsonpath='{.status.currentPrimary}')
kubectl --context kind-rmk8soperator -n tak-operator-system \
  exec "$primary" -c postgres -- psql -d cot -c \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
```

After verification, remove only the old Deployment, its Service and the temporary
import policy. Keep PVC `tak-database` as a recovery copy; CNPG uses its own PVCs.
The existing TAK data volume, certificates and initialization marker remain valid.

```sh
kubectl --context kind-rmk8soperator -n tak-operator-system \
  delete deployment/tak-database service/tak-database \
  networkpolicy/tak-database-legacy-import
task up
```

If import fails, inspect the CNPG bootstrap Job logs and leave the old database
and its PVC intact. Resume the old TAK workload only after restoring ingress
from Pods labelled `app.kubernetes.io/name=takserver` on port 5432 to that old
database; the temporary import policy allows only the CNPG importer.

References: [CNPG installation](https://cloudnative-pg.io/docs/1.30/installation_upgrade/),
[PostGIS images](https://github.com/cloudnative-pg/postgis-containers),
[logical imports](https://cloudnative-pg.io/docs/1.30/database_import/), and
[networking](https://cloudnative-pg.io/docs/1.30/networking/).
