# The Octopus side of issue #65: space, environments, lifecycle, feeds and the project shell. The deployment process,
# runbooks and non-sensitive variables are Config as Code in .octopus/project/ (Octopus writes them there); sensitive
# variables and Tentacle targets are not in Terraform (targets register themselves with `tentacle register-with`).

resource "octopusdeploy_space" "argus" {
  name                        = "Argus"
  description                 = "The Argus suite's self-hosted services."
  space_managers_team_members = var.space_managers
}

locals {
  space_id = octopusdeploy_space.argus.id
}

# Staging: a report-only second broker on the broker host (IRIS_REPORT_ONLY), no agents, no secrets.
resource "octopusdeploy_environment" "staging" {
  space_id    = local.space_id
  name        = "Staging"
  description = "Report-only broker on :8121 beside production. Catches bad images, config and policy."
  sort_order  = 1
}

resource "octopusdeploy_environment" "production" {
  space_id           = local.space_id
  name               = "Production"
  description        = "The live broker on :8120 and an agent on every GPU node."
  sort_order         = 2
  use_guided_failure = true # a failed step waits for a person instead of leaving the agents half-rolled
}

# Staging then Production, both promoted by hand.
resource "octopusdeploy_lifecycle" "iris" {
  space_id    = local.space_id
  name        = "Staging then Production"
  description = "Every release goes through the report-only staging broker first."

  phase {
    name                        = "Staging"
    optional_deployment_targets = [octopusdeploy_environment.staging.id]
  }
  phase {
    name                        = "Production"
    optional_deployment_targets = [octopusdeploy_environment.production.id]
  }

  release_retention_with_strategy {
    strategy         = "Count"
    quantity_to_keep = 10
    unit             = "Items"
  }
  tentacle_retention_with_strategy {
    strategy         = "Count"
    quantity_to_keep = 3
    unit             = "Items"
  }
}

# The image. Public, so no credentials; tags carry no `v` (0.2.2), which is what IRIS_TAG needs.
resource "octopusdeploy_docker_container_registry" "ghcr" {
  space_id    = local.space_id
  name        = "GHCR"
  feed_uri    = "https://ghcr.io"
  api_version = "v2"
}

# The deploy files (compose files, .octopus/scripts) at the same version: a release tag's source archive.
resource "octopusdeploy_github_repository_feed" "github" {
  space_id = local.space_id
  name     = "GitHub releases"
  feed_uri = "https://api.github.com"
}

resource "octopusdeploy_git_credential" "github" {
  space_id    = local.space_id
  name        = "argus-iris (GitHub)"
  description = "Config as Code for argus-iris; scoped to that one repository."
  username    = var.github_username
  password    = var.github_token
  repository_restrictions = {
    enabled              = true
    allowed_repositories = [var.github_repo]
  }
}

resource "octopusdeploy_project_group" "argus" {
  space_id = local.space_id
  name     = "Argus"
}

resource "octopusdeploy_project" "iris" {
  space_id         = local.space_id
  name             = "argus-iris"
  description      = "VRAM-aware GPU lease broker: the broker on morpheus, an agent on every GPU node. Issue #65."
  lifecycle_id     = octopusdeploy_lifecycle.iris.id
  project_group_id = octopusdeploy_project_group.argus.id

  # Changes to the process from the Octopus UI are committed to a branch and reach dev through a PR, like any other
  # change: both long-lived branches are protected.
  git_library_persistence_settings {
    git_credential_id  = octopusdeploy_git_credential.github.id
    url                = var.github_repo
    base_path          = ".octopus/project" # Octopus writes its OCL here, apart from the scripts
    default_branch     = "dev"
    protected_branches = ["dev", "main"]
  }

  connectivity_policy {
    allow_deployments_to_no_targets = false
    exclude_unhealthy_targets       = false
    skip_machine_behavior           = "None"
    target_roles                    = ["iris-broker", "iris-agent"]
  }
}

data "octopusdeploy_channels" "iris" {
  space_id   = local.space_id
  project_id = octopusdeploy_project.iris.id
  take       = 1
}

# A new image version on GHCR creates a release, so the release never races the image build.
resource "octopusdeploy_external_feed_create_release_trigger" "ghcr" {
  count      = var.release_trigger ? 1 : 0
  space_id   = local.space_id
  name       = "New argus-iris image"
  project_id = octopusdeploy_project.iris.id
  channel_id = data.octopusdeploy_channels.iris.channels[0].id
  package {
    deployment_action_slug = var.release_trigger_step
    package_reference      = "argus-iris"
  }
}
