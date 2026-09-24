## SSH access

Follow the instructions on this page to access your IDEA cluster via SSH. **Port TCP/22** must be open on your corporate firewall.

### SSM Access

AWS System Manager Session Manager (SSM) is enabled on your IDEA cluster. Users with access to the AWS console can access any EC2 resources via SSH over SSM:

- Navigate to EC2 console
- Select the EC2 instance you want to access
- Click Connect button
- Click Session Manager tab

### Post-quantum key exchange

Supported Linux hosts prefer the post-quantum algorithms available in their installed SSH software. The controller checks running desktops every six hours and attempts at most five refreshes per pass. Only READY Linux desktops with a server instance and an older or missing bootstrap refresh version are eligible. Larger fleets need multiple passes. The SSH service restarts after configuration validation; open SSH sessions stay connected.
