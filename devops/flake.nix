# SPDX-License-Identifier: MIT
# Development environment for the LibreLane notebook web UI (Django API + Next.js).
{
  description = "LibreLane Colab notebook as a Django + Next.js web application";

  nixConfig = {
    extra-substituters = "https://nix-cache.fossi-foundation.org";
    extra-trusted-public-keys = "nix-cache.fossi-foundation.org:3+K59iFwXqKsL7BNu6Guy0v+uTlwsxYQxjspXzqLYQs=";
  };

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";

    librelane.url = "github:librelane/librelane/1e4f4d5bf9d2693798b12dc0c1cd0337ad266a0d";
    librelane.inputs.nix-eda.url = "github:fossi-foundation/nix-eda/6.11.0";
    librelane.inputs.nix-eda.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs =
    { self, nixpkgs, librelane, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      projectRoot = toString ../.;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = librelane.legacyPackages.${system};
          web = import ./web-shell.nix {
            inherit pkgs;
            lib = pkgs.lib;
            inherit projectRoot;
          };
        in
        {
          default = web.shell;
        }
      );

      packages = forAllSystems (
        system:
        let
          pkgs = librelane.legacyPackages.${system};
          web = import ./web-shell.nix {
            inherit pkgs;
            lib = pkgs.lib;
            inherit projectRoot;
          };
        in
        {
          inherit (web) manage web;
          default = web.web;
          python = web.webPython;
        }
      );

      apps = forAllSystems (
        system:
        let
          web = self.packages.${system}.web;
        in
        {
          default = {
            type = "app";
            program = "${web}/bin/librelane-web";
          };
        }
      );

      formatter = librelane.formatter;
    };
}
