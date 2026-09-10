# Repository privacy

- Before committing or pushing, inspect staged changes and new files for
  credentials and machine-specific information.
- Do not commit real local or private IP addresses, `.local` hostnames, personal
  usernames, workstation home paths, Wi-Fi details, device identifiers, or
  credentials. Use descriptive placeholders such as `<device-host>`,
  `<device-user>`, `$HOME`, or `$CM5_USER`. If a numeric IP is required in an
  example, use an address reserved for documentation.
- Keep secrets and local configuration outside the repository. Example values
  must be unmistakably synthetic.
- Record vendored-source provenance with a project name, upstream URL, version,
  or commit rather than an absolute local checkout path.
