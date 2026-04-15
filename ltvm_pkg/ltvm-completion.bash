# Bash tab completion for ltvm
# Installed by: ltvm install

_ltvm_completions() {
	local cur prev commands cluster_actions
	cur="${COMP_WORDS[COMP_CWORD]}"
	prev="${COMP_WORDS[COMP_CWORD-1]}"

	commands="install build-all build-container build-kernel
		build-image build-lustre build-shell build-status fetch
		create ensure destroy start stop list ssh console-log
		nmi crash-collect snapshot restore doctor deploy-lustre exec
		cluster"

	cluster_actions="create destroy deploy status exec list ssh"

	# Complete subcommand name
	if [[ $COMP_CWORD -eq 1 ]]; then
		COMPREPLY=($(compgen -W "$commands" -- "$cur"))
		return
	fi

	# Complete cluster sub-actions
	if [[ "${COMP_WORDS[1]}" == "cluster" && $COMP_CWORD -eq 2 ]]; then
		COMPREPLY=($(compgen -W "$cluster_actions" -- "$cur"))
		return
	fi

	# Complete VM names for commands that take them
	case "${COMP_WORDS[1]}" in
		destroy|start|stop|ssh|exec|deploy-lustre|console-log| \
		nmi|crash-collect|snapshot|restore)
			if [[ $COMP_CWORD -eq 2 ]]; then
				local vms
				vms=$(ltvm list 2>/dev/null | awk 'NR>2 && NF {print $1}')
				COMPREPLY=($(compgen -W "$vms" -- "$cur"))
				return
			fi
			;;
	esac

	# Complete flags
	case "${COMP_WORDS[1]}" in
		build-all|build-kernel|build-lustre)
			COMPREPLY=($(compgen -W "--lustre-tree --force --json -v --kernel" -- "$cur"))
			;;
		build-container|build-image)
			COMPREPLY=($(compgen -W "--force --json -v" -- "$cur"))
			;;
		create|ensure)
			COMPREPLY=($(compgen -W "--vcpus --mem --ip --target --mdt-disks --ost-disks --disk-size --json -v" -- "$cur"))
			;;
		deploy-lustre)
			COMPREPLY=($(compgen -W "--build --mount --target --json -v" -- "$cur"))
			;;
		install|setup)
			COMPREPLY=($(compgen -W "--qemu --network --install --ssh --verify --force --subnet" -- "$cur"))
			;;
		exec)
			COMPREPLY=($(compgen -W "--timeout --json -v" -- "$cur"))
			;;
		*)
			COMPREPLY=($(compgen -W "--json -v" -- "$cur"))
			;;
	esac
}

complete -F _ltvm_completions ltvm
