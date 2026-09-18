variable "IDEA_VERSION" { default = "" }

group "default" { targets = ["control-plane"] }

target "control-plane" {
  context = "."
  dockerfile = "deployment/ecr/idea-control-plane/Dockerfile"
  tags = ["idea-control-plane-ci:latest"]
  args = { IDEA_VERSION = IDEA_VERSION }
  contexts = { dcv-packages = "/tmp/dcv-packages" }
  cache-from = ["type=gha,scope=idea-control-plane"]
  cache-to = ["type=gha,mode=max,scope=idea-control-plane"]
}
