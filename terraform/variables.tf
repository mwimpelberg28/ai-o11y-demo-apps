variable "region" {
  description = "AWS region to deploy the EKS cluster into."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "Name of the EKS cluster."
  type        = string
  default     = "ai-o11y-demo"
}

variable "kubernetes_version" {
  description = "Kubernetes control-plane version."
  type        = string
  default     = "1.32"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.0.0.0/16"
}

variable "node_instance_types" {
  description = "Instance types for the managed node group. The demo runs 5 app namespaces plus loadgen and Postgres."
  type        = list(string)
  default     = ["t3.large"]
}

variable "node_desired_size" {
  description = "Desired number of worker nodes."
  type        = number
  default     = 3
}

variable "node_min_size" {
  description = "Minimum number of worker nodes."
  type        = number
  default     = 2
}

variable "node_max_size" {
  description = "Maximum number of worker nodes."
  type        = number
  default     = 4
}

variable "enable_argocd" {
  description = "Install Argo CD onto the cluster via Helm. The demo chart itself is deployed by an Argo CD Application generated from .env (see tools/generate-argocd-app.sh), not by Terraform."
  type        = bool
  default     = true
}

variable "argocd_chart_version" {
  description = "Version of the argo-cd Helm chart (argoproj/argo-helm)."
  type        = string
  default     = "7.7.11"
}

variable "tags" {
  description = "Tags applied to all resources."
  type        = map(string)
  default = {
    Project = "ai-o11y-demo-apps"
  }
}
