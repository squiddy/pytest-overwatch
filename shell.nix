{ pkgs ? import <nixpkgs> {}}:

pkgs.mkShell {
  packages = [ pkgs.poetry pkgs.python38 pkgs.just ];
}
