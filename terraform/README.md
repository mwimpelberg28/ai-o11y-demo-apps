# terraform/ — EKS cluster for the demo apps

Provisions an EKS cluster ready to run this repo's Helm chart, using the
community `terraform-aws-modules`. What it creates:

- **VPC** — 3 AZs, public + private subnets, single NAT gateway, subnets tagged
  for EKS load-balancer discovery.
- **EKS cluster** — public endpoint, `enable_cluster_creator_admin_permissions`
  so the identity running `terraform apply` gets cluster-admin immediately.
- **Managed node group** — on-demand, defaults to 3× `t3.large` (min 2 / max 4).
- **EBS CSI driver addon + IRSA role** — the "right role". The
  `aws-ebs-csi-driver` addon runs as `kube-system:ebs-csi-controller-sa`, which
  assumes a dedicated IAM role (via OIDC/IRSA) with the EBS CSI policy attached.
  Without this, the chart's `data-postgres-0` PVC can't provision an EBS volume
  and Postgres never starts.

## Usage

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars   # optional — edit defaults
terraform init
terraform apply

# Point kubectl at the new cluster (terraform prints this exact command):
aws eks update-kubeconfig --region us-east-1 --name ai-o11y-demo
```

Requires AWS credentials on your environment (`aws sts get-caller-identity`
should succeed) and `terraform >= 1.5`.

## Default StorageClass (do this before installing the chart)

EKS ships a `gp2` StorageClass that is **not** marked default, and the chart's
Postgres PVC leaves `storageClassName` empty — so it needs a default. The EBS
CSI role above makes provisioning *possible*; you still pick the default class.
Easiest path, since the addon is already installed:

```bash
kubectl apply -f gp3-storageclass.yaml   # marks gp3 as the default
kubectl get storageclass                 # confirm exactly one (default)
```

(Alternatively mark gp2 default, or set `postgres.storage.storageClass` in your
Helm values — see the root README prereqs.)

## Then install the apps

```bash
cd ..
./tools/install.sh && ./tools/verify.sh
```

## Teardown

```bash
# Remove the apps first so their LoadBalancer Services release ELBs/ENIs,
# otherwise VPC destroy can hang:
./tools/uninstall.sh
cd terraform && terraform destroy
```
