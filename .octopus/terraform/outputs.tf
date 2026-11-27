output "space_id" {
  value = octopusdeploy_space.argus.id
}

output "project_id" {
  value = octopusdeploy_project.iris.id
}

output "environments" {
  value = {
    staging    = octopusdeploy_environment.staging.id
    production = octopusdeploy_environment.production.id
  }
}
