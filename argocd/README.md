# argocd/ — GitOps deploy of the demo apps

Deploy ai-o11y-demo-apps with Argo CD instead of `tools/install.sh`'s
imperative `helm upgrade`. Argo CD pulls the chart from git and keeps the
cluster reconciled to it.

## Prerequisites

- An Argo CD install. The [`terraform/`](../terraform/) stack bootstraps one
  (`enable_argocd = true`, the default). Otherwise install it yourself.
- A populated `.env` — `tools/install.sh` writes it (answer the prompts, then
  reply `n` at the "Continue?" gate to skip the imperative helm install). The
  generator reuses the exact same keys.

## End-to-end flow

Assumes the [`terraform/`](../terraform/) stack is applied (EKS + Argo CD up).

```bash
# 1. Point kubectl at the cluster (matches the `configure_kubectl` TF output).
aws eks update-kubeconfig --region us-west-2 --name ai-o11y-demo

# 2. Set a default StorageClass — EKS's gp2 is non-default, so the Postgres
#    PVC stays Pending without this.
kubectl apply -f terraform/gp3-storageclass.yaml
kubectl get storageclass            # exactly one should show (default)

# 3. Commit the synthetic user pool to the branch Argo tracks (see gotcha #2).
#    Without it loadgen comes up empty and the demo produces no traffic.
python3 tools/regenerate-users.py --seed 42 --out helm/config/users.yaml   # if not already present
git add -f helm/config/users.yaml && git commit -m "add user pool" && git push fork feat/eks-terraform-argocd

# 4. Generate the Application from .env (secrets inlined, file gitignored).
#    Defaults already point at this fork/branch — see "Source repo" below.
./tools/generate-argocd-app.sh

# 5. Apply the Application and wait for Synced + Healthy.
kubectl apply -f argocd/application.generated.yaml
kubectl -n argocd get application ai-o11y-demo-apps -w

# 6. Verify health (9 checks, exits 0 when green).
./tools/verify.sh
```

Give the apps ~10 minutes to produce dashboard-visible volume.

## Access the apps

```bash
# NeonCart e-commerce UI  ->  http://localhost:8080
kubectl -n neoncart    port-forward svc/neoncart-web   8080:8000

# SupportBot helpdesk UI  ->  http://localhost:8081
kubectl -n support-bot port-forward svc/supportbot-web 8081:8000

# Argo CD UI  ->  https://localhost:8082  (user: admin)
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
kubectl -n argocd port-forward svc/argocd-server 8082:443
```

Each `port-forward` runs until you Ctrl-C it, so use a separate terminal per UI.

## Source repo

The generator defaults `ARGOCD_REPO_URL`/`ARGOCD_TARGET_REVISION` to this fork
and branch, so Argo pulls the chart (including the committed user pool from
step 3) from there. Override for a different fork/branch:

```bash
ARGOCD_REPO_URL=https://github.com/you/your-fork \
ARGOCD_TARGET_REVISION=my-branch \
./tools/generate-argocd-app.sh
```

`application.yaml.example` is a committed, secret-free reference of the shape.
The real, secret-bearing manifest is `application.generated.yaml` (gitignored).

## Two GitOps gotchas this setup handles

**1. The Postgres password must be explicit.** Argo CD renders Helm with
`helm template`, where the chart's `lookup`-based password persistence (in
`helm/templates/secrets.yaml`) always returns empty. Left alone, `randAlphaNum`
re-rolls the password on every sync while `postgres-0`'s PVC keeps the original
— so all apps lose DB auth. The generator sets `postgres.password` explicitly
and persists it to `.env` (`POSTGRES_PASSWORD`) so it's stable across syncs.

**2. The user pool must be in git.** `helm/config/users.yaml` (the synthetic
shopper/employee identities that drive loadgen) is gitignored, but the chart
reads it via `.Files.Get` at render time. Argo CD pulls the chart from git, so
if the file isn't committed to the tracked revision, loadgen comes up with an
empty pool and produces **no traffic** — the demo looks dead. Force-add and
commit it (step 3 above). It's non-secret faker-generated data.

## Secrets handling

Secrets reach the chart via the Application's `spec.source.helm.valuesObject`,
generated from `.env`. They live in the Application object in the cluster (and
in `application.generated.yaml` locally) — never in git, and never in Terraform
state. For a hands-off-git posture, swap to the Argo CD Vault Plugin / External
Secrets / sealed-secrets later; this flow keeps the demo a single command.

## Teardown

```bash
kubectl delete -f argocd/application.generated.yaml   # cascades to the apps
```
