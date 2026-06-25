# ---------------------------------------------------------------------------
# Argo CD — the GitOps control plane. Terraform installs the *platform*; it
# does NOT define the demo app here. The demo's Argo CD Application carries
# secrets (Anthropic key, OTLP creds, Postgres password) and is generated from
# .env by tools/generate-argocd-app.sh, then `kubectl apply`-ed — keeping
# secrets out of both git and Terraform state.
# ---------------------------------------------------------------------------
resource "helm_release" "argocd" {
  count = var.enable_argocd ? 1 : 0

  name             = "argocd"
  namespace        = "argocd"
  create_namespace = true

  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  version    = var.argocd_chart_version

  # Wait for the cluster's EBS-backed default storage etc. to settle first.
  depends_on = [module.eks]
}
