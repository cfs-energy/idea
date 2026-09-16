variable "IDEA_VERSION" { default = "" }

group "default" { targets = ["scheduler", "control-plane"] }

target "scheduler" {
  context = "deployment/ecr/idea-scheduler-pbs"
  tags = ["idea-scheduler-ci:latest"]
  args = { IDEA_VERSION = IDEA_VERSION }
  cache-from = ["type=gha,scope=idea-scheduler-pbs"]
  cache-to = ["type=gha,mode=max,scope=idea-scheduler-pbs"]
}

target "control-plane" {
  context = "."
  dockerfile = "deployment/ecr/idea-control-plane/Dockerfile"
  tags = ["idea-control-plane-ci:latest"]
  args = { IDEA_VERSION = IDEA_VERSION, PBS_IMAGE = "ci-pbs" }
  // A target context shares the freshly built scheduler without publishing an intermediate image.
  contexts = { ci-pbs = "target:scheduler", dcv-packages = "/tmp/dcv-packages" }
  cache-from = ["type=gha,scope=idea-control-plane"]
  cache-to = ["type=gha,mode=max,scope=idea-control-plane"]
}
