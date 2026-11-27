terraform {
  required_version = ">= 1.6"
  required_providers {
    octopusdeploy = {
      source  = "OctopusDeploy/octopusdeploy"
      version = "~> 1.21.2"
    }
  }
  # State holds the Git credential's token: keep it out of git (.gitignore). For shared state, an S3-compatible bucket
  # (Cloudflare R2) works with `backend "s3"` and -backend-config; see README.md.
}

provider "octopusdeploy" {
  address = var.octopus_url
  api_key = var.octopus_api_key
}

# The same instance, defaulting to the Argus space. The release trigger's read ignores its own space_id and asks the
# provider's default space (provider 1.21.2 and main as of 2026-11-27), which a key scoped to Argus may not read.
provider "octopusdeploy" {
  alias    = "argus"
  address  = var.octopus_url
  api_key  = var.octopus_api_key
  space_id = local.space_id
}
