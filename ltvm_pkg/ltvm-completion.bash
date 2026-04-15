# Bash tab completion for ltvm
# Installed by: ltvm install

_ltvm_completions() {
	local cur prev commands cluster_actions
	cur="${COMP_WORDS[COMP_CWORD]}"
	prev="${COMP_WORDS[COMP_CWORD-1]}"

	commands="install update build target vm
		create destroy start stop list
		doctor deploy-lustre cluster llmount"

	cluster_actions="create destroy deploy status exec list ssh"
	target_actions="list show clean validate fetch package publish"
	build_actions="all container kernel image lustre shell status"
	vm_actions="console-log crash-collect nmi snapshot restore"

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

	# Complete target sub-actions
	if [[ "${COMP_WORDS[1]}" == "target" && $COMP_CWORD -eq 2 ]]; then
		COMPREPLY=($(compgen -W "$target_actions" -- "$cur"))
		return
	fi

	# Complete build sub-actions
	if [[ "${COMP_WORDS[1]}" == "build" && $COMP_CWORD -eq 2 ]]; then
		COMPREPLY=($(compgen -W "$build_actions" -- "$cur"))
		return
	fi

	# Complete vm sub-actions
	if [[ "${COMP_WORDS[1]}" == "vm" && $COMP_CWORD -eq 2 ]]; then
		COMPREPLY=($(compgen -W "$vm_actions" -- "$cur"))
		return
	fi

	# VM names: `vm <action> <name>` (words[1]=vm, words[2]=action, words[3]=name)
	if [[ "${COMP_WORDS[1]}" == "vm" && $COMP_CWORD -eq 3 ]]; then
		local vms
		vms=$(ltvm list 2>/dev/null | awk 'NR>2 && NF {print $1}')
		COMPREPLY=($(compgen -W "$vms" -- "$cur"))
		return
	fi

	# Complete VM names for top-level commands that take them
	case "${COMP_WORDS[1]}" in
		destroy|start|stop|deploy-lustre|llmount)
			if [[ $COMP_CWORD -eq 2 ]]; then
				local vms
				vms=$(ltvm list 2>/dev/null | awk 'NR>2 && NF {print $1}')
				COMPREPLY=($(compgen -W "$vms" -- "$cur"))
				return
			fi
			;;
	esac

	# Complete flags (for `build <action>`, key on WORDS[2])
	if [[ "${COMP_WORDS[1]}" == "build" ]]; then
		case "${COMP_WORDS[2]}" in
			all|kernel|lustre)
				COMPREPLY=($(compgen -W "--lustre-tree --force --json -v --kernel" -- "$cur"))
				return
				;;
			container|image)
				COMPREPLY=($(compgen -W "--force --json -v" -- "$cur"))
				return
				;;
		esac
	fi

	case "${COMP_WORDS[1]}" in
		create)
			COMPREPLY=($(compgen -W "--vcpus --mem --ip --target --mdt-disks --ost-disks --disk-size --json -v" -- "$cur"))
			;;
		deploy-lustre)
			COMPREPLY=($(compgen -W "--build --mount --target --json -v" -- "$cur"))
			;;
		install|setup)
			COMPREPLY=($(compgen -W "--qemu --network --install --ssh --verify --force --subnet" -- "$cur"))
			;;
		*)
			COMPREPLY=($(compgen -W "--json -v" -- "$cur"))
			;;
	esac
}

complete -F _ltvm_completions ltvm
