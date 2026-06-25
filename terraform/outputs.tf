output "cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "region" {
  description = "AWS region the cluster lives in."
  value       = var.region
}

output "configure_kubectl" {
  description = "Run this to point kubectl at the new cluster."
  value       = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name}"
}

output "ebs_csi_irsa_role_arn" {
  description = "IAM role ARN assumed by the EBS CSI driver controller."
  value       = module.ebs_csi_irsa_role.iam_role_arn
}

output "argocd_installed" {
  description = "Whether Argo CD was installed by this stack."
  value       = var.enable_argocd
}

output "argocd_admin_password_cmd" {
  description = "Fetch the initial Argo CD admin password (user: admin)."
  value       = var.enable_argocd ? "kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo" : "Argo CD not installed (enable_argocd = false)."
}

output "argocd_port_forward_cmd" {
  description = "Open the Argo CD UI locally."
  value       = var.enable_argocd ? "kubectl -n argocd port-forward svc/argocd-server 8082:443  # https://localhost:8082" : "Argo CD not installed (enable_argocd = false)."
}
