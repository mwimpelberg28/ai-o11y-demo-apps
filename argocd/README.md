# argocd/ — GitOps deploy of the demo apps

Deploy ai-o11y-demo-apps with Argo CD instead of `tools/install.sh`'s
imperative `helm upgrade`. Argo CD pulls the chart from git and keeps the
cluster reconciled to it.

## Prerequisites

- An Argo CD install. The [`terraform/`](../terraform/) stack bootstraps one
  (`enable_argocd = true`, the default). Otherwise install it yourself.
- A populated `.env` — `tools/install.sh` writes it, or create it by hand.
  The generator reuses the exact same keys.

## Flow

```bash
# 1. Generate the Application from .env (secrets inlined, file gitignored).
./tools/generate-argocd-app.sh

# 2. Commit the synthetic user pool to the branch Argo tracks (see gotcha #2).
git add -f helm/config/users.yaml && git commit -m "add user pool" && git push

# 3. Apply the Application and watch it sync.
kubectl apply -f argocd/application.generated.yaml
kubectl -n argocd get application ai-o11y-demo-apps -w
```

`application.yaml.example` is a committed, secret-free reference of the shape.
The real, secret-bearing manifest is `application.generated.yaml` (gitignored).

To point at a fork/branch, set these before running the generator:

```bash
ARGOCD_REPO_URL=https://github.com/you/your-fork \
ARGOCD_TARGET_REVISION=my-branch \
./tools/generate-argocd-app.sh
```

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
commit it (step 2 above). It's non-secret faker-generated data.

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
