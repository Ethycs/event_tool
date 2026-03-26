use pixi instead of pip, for example, pixi run python
Initial setup:
pixi init --format pyproject

pixi#
Description#
The pixi command is the main entry point for the Pixi CLI.

Usage#

pixi [OPTIONS] <COMMAND>
Subcommands#
Command	Description
init	Creates a new workspace
add	Adds dependencies to the workspace
remove	Removes dependencies from the workspace
install	Install an environment, both updating the lockfile and installing the environment
reinstall	Re-install an environment, both updating the lockfile and re-installing the environment
update	The update command checks if there are newer versions of the dependencies and updates the pixi.lock file and environments accordingly
upgrade	Checks if there are newer versions of the dependencies and upgrades them in the lockfile and manifest file
lock	Solve environment and update the lock file without installing the environments
run	Runs task in the pixi environment
exec	Run a command and install it in a temporary environment
shell	Start a shell in a pixi environment, run exit to leave the shell
shell-hook	Print the pixi environment activation script
workspace	Modify the workspace configuration file through the command line
task	Interact with tasks in the workspace
list	List workspace's packages
tree	Show a tree of workspace dependencies
global	Subcommand for global package management actions
auth	Login to prefix.dev or anaconda.org servers to access private channels
config	Configuration management
info	Information about the system, workspace and environments for the current machine
upload	Upload a conda package
search	Search a conda package
self-update	Update pixi to the latest version or a specific version
clean	Cleanup the environments
completion	Generates a completion script for a shell
build	Workspace configuration


Global Options#
--help (-h)
Display help information
--verbose (-v)
Increase logging verbosity (-v for warnings, -vv for info, -vvv for debug, -vvvv for trace)
--quiet (-q)
Decrease logging verbosity (quiet mode)
--color <COLOR>
Whether the log needs to be colored
env: PIXI_COLOR
default: auto
options: always, never, auto
--no-progress
Hide all progress bars, always turned on if stderr is not a terminal
env: PIXI_NO_PROGRESS
default: false