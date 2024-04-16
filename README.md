# Integrated Digital Engineering on AWS

**Read Me First**

IDEA is **NOT** supported by AWS. Do **NOT** expect support from AWS for IDEA. If you're looking for a supported solution, please look to AWS RES.

AWS has decided to take a different direction with IDEA and pursue RES (https://github.com/aws/res). Until RES achieves feature parity with IDEA, this repository will be the authoritative source for IDEA updates, patches, and information.

This project is NOT AWS RES (https://github.com/aws/res). RES is a fork of IDEA and not all IDEA features made it to RES. Specifically HPC and other features. RES Documentation is available on https://docs.aws.amazon.com/res/latest/ug/overview.html

This code was originally written by AWS but has been abandoned and taken open source as of version 3.1.6 respecting the original Apache 2.0 license terms.

## Documentation

https://docs.idea-hpc.com/

## What is IDEA?

Integrated Digital Engineering on AWS (IDEA) is an evolution of SOCA ([Scale Out Computing on AWS](https://awslabs.github.io/scale-out-computing-on-aws/)). IDEA empowers teams of engineers, scientists and researchers with a cloud environment to host engineering tools required for end-to-end product development workloads.

IDEA supports two main computation workstyles. **eVDI** (Virtual Desktops) & **Scale Out Compute / HPC** (OpenPBS based job submission)

IDEA provides all the backend infrastructure to support these worksryles on AWS with a simple to use web interface, SSO, and more advanced user features.

IDEA can be a good solution for any compute or VDI workflow but often specializes in:
* Computer Aided Design – CAD
* Computer Aided Engineering - CAE
* Model Based Systems Engineering - MBSE
* Electronic Design Automation – EDA Using this solution, research and development (R&D) leaders enable engineers and designers to break-through development silos and accelerate the product development process.
* Large distributed compute and simulation jobs. 1-100k+ CPU and GPU cores. CFD/EFA/etc

## Installation

Refer to [IDEA Installation](https://docs.idea-hpc.com/first-time-users/install-idea) for installation instructions.

This solution collects anonymous operational metrics to help AWS improve the quality of the solution.

***

Copyright 2022 Amazon.com, Inc. or its affiliates. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
