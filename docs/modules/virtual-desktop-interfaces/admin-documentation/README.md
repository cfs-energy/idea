# Admin Documentation

VDI administrators have extensive capabilities to manage virtual desktop environments in IDEA. This section covers the key administrative tasks and features.

{% hint style="info" %}
**New Feature**: All tables in the Virtual Desktop Interfaces module now support alphanumeric sorting, making it easier to find and organize resources. Simply click on any column header to sort the table by that column.
{% endhint %}

## Administrative Features

* [Session Management](sessions.md) - View, control, and launch sessions on behalf of users
* [Software Stack Management](virtual-desktop-images-software-stacks.md) - Create and manage AMIs with pre-installed applications
* [Permission Management](permissions-management.md) - Configure access control for shared sessions

## Key Administrative Capabilities

### Software Stack Management

Administrators can now manage software stacks more efficiently with the new enable/disable/delete functionality and per-stack instance type restrictions. This allows finer-grained control over what compute resources users can select for each software stack. See [Software Stack Management](virtual-desktop-images-software-stacks.md) for details.

### Session Administration

Administrators can now launch sessions on behalf of users with instance types not defined in the regular allowed lists. This provides flexibility for special use cases while maintaining restrictions for regular users. See [Session Management](sessions.md) for details.

### Form Enhancements

The Create Session form has been refactored to only show users what they have permission to launch, making the session creation process more intuitive and reducing permission errors.

## Related Topics

For user documentation, refer to the [User Documentation](../user-documentation/) section.
