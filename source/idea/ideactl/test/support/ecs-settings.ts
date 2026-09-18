/**
 * The container-module host and task sizings that every ECS synthesis fixture carries.
 * Spread into a settings object; override individual keys after the spread.
 */

export const ECS_HOST_SETTINGS = {
  "ecs.hosts.instance_type": "m7g.large",
  "ecs.hosts.max": 4,
  "ecs.hosts.min": 3,
  "ecs.hosts.volume_size": 60,
};

export const ECS_TASK_SETTINGS = {
  "ecs.tasks.bastion-host.cpu": 256,
  "ecs.tasks.bastion-host.desired": 2,
  "ecs.tasks.bastion-host.memory": 512,
  "ecs.tasks.cluster-manager.cpu": 256,
  "ecs.tasks.cluster-manager.desired": 2,
  "ecs.tasks.cluster-manager.memory": 1024,
  "ecs.tasks.dcv-broker.cpu": 512,
  "ecs.tasks.dcv-broker.desired": 2,
  "ecs.tasks.dcv-broker.memory": 4096,
  "ecs.tasks.dcv-gateway.cpu": 256,
  "ecs.tasks.dcv-gateway.desired": 2,
  "ecs.tasks.dcv-gateway.memory": 512,
  "ecs.tasks.scheduler.cpu": 512,
  "ecs.tasks.scheduler.desired": 1,
  "ecs.tasks.scheduler.memory": 2048,
  "ecs.tasks.vdc.cpu": 256,
  "ecs.tasks.vdc.desired": 2,
  "ecs.tasks.vdc.memory": 1024,
};
