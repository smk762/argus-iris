variable "octopus_url" {
  description = "Octopus Cloud instance, e.g. https://example.octopus.app"
  type        = string
}

variable "octopus_api_key" {
  description = "API key of a user who may create spaces (TF_VAR_octopus_api_key)."
  type        = string
  sensitive   = true
}

variable "space_managers" {
  description = "Octopus user ids (Users-…) who manage the Argus space."
  type        = list(string)
}

variable "github_repo" {
  description = "The repository whose .octopus/ holds the deployment process (Config as Code)."
  type        = string
  default     = "https://github.com/smk762/argus-iris.git"
}

variable "github_username" {
  description = "Owner of the token below."
  type        = string
  default     = "smk762"
}

variable "github_token" {
  description = "Fine-grained GitHub token, this repository only, Contents read/write: Octopus commits process changes on a branch (TF_VAR_github_token)."
  type        = string
  sensitive   = true
}

variable "git_default_branch" {
  description = "Branch Octopus reads the process from. Converting the project to Git commits its first OCL there, and dev is protected (in Octopus and on GitHub), so bootstrap on a feature branch, merge it, then switch to dev."
  type        = string
  default     = "dev"
}

variable "release_trigger" {
  description = "Create the GHCR release trigger. Leave false until the process has its first step: the trigger names that step's package."
  type        = bool
  default     = false
}

variable "release_trigger_step" {
  description = "Slug of the process step whose argus-iris package the release trigger watches."
  type        = string
  default     = "render-broker-config"
}

variable "health_check_trigger" {
  description = "Schedule the Health check runbook hourly. Leave false until its OCL is on the default branch."
  type        = bool
  default     = false
}
