<script lang="ts">
	import { onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { confirmMemoryById, deleteMemoryById, getMemories } from '$lib/apis/memories';
	import MemoryModal from '$lib/components/chat/Settings/Personalization/MemoryModal.svelte';
	import ConfirmDialog from '$lib/components/common/ConfirmDialog.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import CheckIcon from '$lib/components/icons/Check.svelte';
	import DownloadIcon from '$lib/components/icons/Download.svelte';
	import EditIcon from '$lib/components/icons/EditPencil.svelte';
	import PlusIcon from '$lib/components/icons/Plus.svelte';
	import SearchIcon from '$lib/components/icons/Search.svelte';
	import SidebarIcon from '$lib/components/icons/Sidebar.svelte';
	import TrashIcon from '$lib/components/icons/Trash.svelte';
	import { showSidebar } from '$lib/stores';

	type MemoryMeta = {
		confirmed?: boolean;
		confirmed_at?: number;
		created_by?: string;
		confidence?: number;
		expires_at?: number;
		chat_id?: string;
		message_id?: string;
		model?: string;
	};

	type Memory = {
		id: string;
		content: string;
		type?: string;
		path?: string;
		meta?: MemoryMeta | null;
		created_at?: number;
		updated_at?: number;
	};

	type Filter = 'all' | 'review' | 'confirmed';

	let memories: Memory[] = [];
	let loading = true;
	let query = '';
	let filter: Filter = 'all';
	let confirming = '';
	let showMemoryModal = false;
	let selectedMemory: Memory | null = null;
	let pendingDelete: Memory | null = null;

	const isConfirmed = (memory: Memory) => {
		if (typeof memory.meta?.confirmed === 'boolean') return memory.meta.confirmed;
		if (memory.meta?.created_by === 'manual') return true;
		if (memory.meta?.created_by) return false;
		return memory.type === 'user';
	};

	const sourceLabel = (memory: Memory) => {
		switch (memory.meta?.created_by) {
			case 'manual':
				return 'Добавлено вами';
			case 'background_review':
				return 'Предложено после разговора';
			case 'tool':
				return 'Запомнено из разговора';
			default:
				return memory.type === 'user' ? 'Добавлено пользователем' : 'Источник не указан';
		}
	};

	const formatDate = (value?: number) => {
		if (!value) return 'дата не указана';
		return new Intl.DateTimeFormat('ru-RU', {
			day: '2-digit',
			month: 'long',
			year: 'numeric'
		}).format(value * 1000);
	};

	const formatConfidence = (value?: number) => {
		if (typeof value !== 'number') return '';
		const percent = value <= 1 ? value * 100 : value;
		return `${Math.round(percent)}%`;
	};

	const loadMemories = async () => {
		loading = true;
		try {
			memories = (await getMemories(localStorage.token)) ?? [];
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		} finally {
			loading = false;
		}
	};

	const addMemory = () => {
		selectedMemory = null;
		showMemoryModal = true;
	};

	const editMemory = (memory: Memory) => {
		selectedMemory = memory;
		showMemoryModal = true;
	};

	const confirmMemory = async (memory: Memory) => {
		confirming = memory.id;
		try {
			const response = await confirmMemoryById(localStorage.token, memory.id);
			if (response?.memory) {
				memories = memories.map((item) => (item.id === memory.id ? response.memory : item));
				toast.success('Запись подтверждена');
			}
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		} finally {
			confirming = '';
		}
	};

	const deleteMemory = async () => {
		if (!pendingDelete) return;
		const memoryId = pendingDelete.id;
		try {
			const deleted = await deleteMemoryById(localStorage.token, memoryId);
			if (deleted) {
				memories = memories.filter((memory) => memory.id !== memoryId);
				toast.success('Запись удалена из памяти');
			}
		} catch (error) {
			toast.error(error instanceof Error ? error.message : String(error));
		} finally {
			pendingDelete = null;
		}
	};

	const exportMemories = () => {
		const payload = {
			exported_at: new Date().toISOString(),
			product: 'ХЕРУВИМ',
			memories: memories.map(({ id, content, type, path, meta, created_at, updated_at }) => ({
				id,
				content,
				type,
				path,
				confirmed: isConfirmed({ id, content, type, path, meta, created_at, updated_at }),
				source: sourceLabel({ id, content, type, path, meta, created_at, updated_at }),
				created_at,
				updated_at,
				meta
			}))
		};
		const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
		const url = URL.createObjectURL(blob);
		const anchor = document.createElement('a');
		anchor.href = url;
		anchor.download = `heruvim-memory-${new Date().toISOString().slice(0, 10)}.json`;
		anchor.click();
		setTimeout(() => URL.revokeObjectURL(url), 1000);
	};

	onMount(loadMemories);

	$: reviewCount = memories.filter((memory) => !isConfirmed(memory)).length;
	$: confirmedCount = memories.length - reviewCount;
	$: filteredMemories = memories
		.filter((memory) => {
			if (filter === 'review') return !isConfirmed(memory);
			if (filter === 'confirmed') return isConfirmed(memory);
			return true;
		})
		.filter((memory) => {
			const value = query.trim().toLowerCase();
			return (
				!value ||
				memory.content.toLowerCase().includes(value) ||
				memory.path?.toLowerCase().includes(value)
			);
		})
		.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
</script>

<svelte:head>
	<title>Что помнит ХЕРУВИМ</title>
</svelte:head>

<div
	class="h-full w-full min-w-0 overflow-y-auto bg-white transition-[max-width] dark:bg-gray-950 {$showSidebar
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''}"
>
	<div class="mx-auto w-full max-w-5xl px-4 py-6 md:px-8 md:py-9">
		<header class="mb-7 flex flex-wrap items-start justify-between gap-4">
			<div class="flex min-w-0 items-start gap-3">
				<button
					type="button"
					class="flex size-11 shrink-0 items-center justify-center rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-900 md:hidden"
					on:click={() => showSidebar.set(true)}
					aria-label="Открыть меню"
					title="Открыть меню"
				>
					<SidebarIcon className="size-5" />
				</button>
				<div class="min-w-0">
					<h1 class="text-2xl font-semibold text-gray-950 dark:text-white md:text-3xl">
						Что помнит ХЕРУВИМ
					</h1>
					<p class="mt-2 text-base text-gray-600 dark:text-gray-300">
						Факты и предпочтения, которые используются в будущих разговорах
					</p>
				</div>
			</div>
			<div class="flex flex-wrap gap-2">
				<button
					type="button"
					class="flex min-h-11 items-center gap-2 rounded-lg border border-gray-300 px-4 text-sm font-medium text-gray-800 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-gray-700 dark:text-gray-100 dark:hover:bg-gray-900"
					disabled={memories.length === 0}
					on:click={exportMemories}
				>
					<DownloadIcon className="size-5" />
					Экспорт
				</button>
				<button
					type="button"
					class="flex min-h-11 items-center gap-2 rounded-lg bg-emerald-700 px-4 text-sm font-semibold text-white hover:bg-emerald-800 dark:bg-emerald-600 dark:hover:bg-emerald-500"
					on:click={addMemory}
				>
					<PlusIcon className="size-5" strokeWidth="2" />
					Добавить факт
				</button>
			</div>
		</header>

		<div
			class="mb-5 grid grid-cols-2 gap-3 border-y border-gray-200 py-4 dark:border-gray-800 md:max-w-md"
		>
			<div>
				<div class="text-2xl font-semibold text-gray-950 dark:text-white">{confirmedCount}</div>
				<div class="text-sm text-gray-600 dark:text-gray-400">Подтверждено</div>
			</div>
			<div>
				<div class="text-2xl font-semibold text-gray-950 dark:text-white">{reviewCount}</div>
				<div class="text-sm text-gray-600 dark:text-gray-400">Нужно проверить</div>
			</div>
		</div>

		<div class="mb-5 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
			<div class="relative w-full md:max-w-xl">
				<SearchIcon className="pointer-events-none absolute left-3 top-3 size-5 text-gray-500" />
				<input
					bind:value={query}
					class="min-h-11 w-full rounded-lg border border-gray-300 bg-white pl-10 pr-3 text-base text-gray-950 outline-none focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20 dark:border-gray-700 dark:bg-gray-900 dark:text-white"
					placeholder="Найти в памяти"
					aria-label="Найти в памяти"
				/>
			</div>
			<div
				class="grid grid-cols-3 rounded-lg border border-gray-300 p-1 dark:border-gray-700"
				aria-label="Фильтр памяти"
			>
				{#each [{ id: 'all', label: 'Все' }, { id: 'review', label: 'Проверить' }, { id: 'confirmed', label: 'Готово' }] as option}
					<button
						type="button"
						class="min-h-9 whitespace-nowrap rounded-md px-3 text-sm font-medium {filter ===
						option.id
							? 'bg-gray-900 text-white dark:bg-white dark:text-gray-950'
							: 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800'}"
						on:click={() => (filter = option.id as Filter)}
					>
						{option.label}
					</button>
				{/each}
			</div>
		</div>

		{#if loading}
			<div class="flex min-h-52 items-center justify-center"><Spinner className="size-7" /></div>
		{:else if filteredMemories.length === 0}
			<div class="border-y border-gray-200 py-12 text-center dark:border-gray-800">
				<p class="text-lg font-medium text-gray-900 dark:text-white">
					{memories.length === 0 ? 'ХЕРУВИМ пока ничего не запомнил' : 'Подходящих записей нет'}
				</p>
				<p class="mt-2 text-sm text-gray-600 dark:text-gray-400">
					{memories.length === 0
						? 'Добавьте важный факт вручную или попросите ХЕРУВИМ запомнить его в чате.'
						: 'Измените запрос или выберите другой фильтр.'}
				</p>
			</div>
		{:else}
			<div class="flex flex-col gap-3">
				{#each filteredMemories as memory (memory.id)}
					<article class="rounded-lg border border-gray-200 p-4 dark:border-gray-800 md:p-5">
						<div class="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
							<div class="min-w-0 flex-1">
								<div class="mb-3 flex flex-wrap items-center gap-2">
									<span
										class="rounded-full px-2.5 py-1 text-xs font-semibold {isConfirmed(memory)
											? 'bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100'
											: 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-100'}"
									>
										{isConfirmed(memory) ? 'Подтверждено' : 'Нужно проверить'}
									</span>
									{#if memory.path}
										<span class="text-sm text-gray-500 dark:text-gray-400"
											>Раздел: {memory.path}</span
										>
									{/if}
								</div>
								<p
									class="whitespace-pre-wrap break-words text-base leading-7 text-gray-950 dark:text-gray-100"
								>
									{memory.content}
								</p>
								<div
									class="mt-4 flex flex-wrap gap-x-5 gap-y-1 text-sm text-gray-500 dark:text-gray-400"
								>
									<span>{sourceLabel(memory)}</span>
									<span>Обновлено: {formatDate(memory.updated_at)}</span>
									{#if formatConfidence(memory.meta?.confidence)}
										<span>Уверенность: {formatConfidence(memory.meta?.confidence)}</span>
									{/if}
									{#if memory.meta?.expires_at}
										<span>Проверить до: {formatDate(memory.meta.expires_at)}</span>
									{/if}
								</div>
							</div>
							<div class="flex shrink-0 flex-wrap gap-2">
								{#if !isConfirmed(memory)}
									<button
										type="button"
										class="flex min-h-10 items-center gap-2 rounded-lg bg-emerald-700 px-3 text-sm font-semibold text-white hover:bg-emerald-800 disabled:opacity-50 dark:bg-emerald-600 dark:hover:bg-emerald-500"
										disabled={confirming === memory.id}
										on:click={() => confirmMemory(memory)}
									>
										<CheckIcon className="size-4" strokeWidth="2" />
										Верно
									</button>
								{/if}
								<button
									type="button"
									class="flex size-10 items-center justify-center rounded-lg border border-gray-300 text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-900"
									on:click={() => editMemory(memory)}
									aria-label="Исправить запись"
									title="Исправить"
								>
									<EditIcon className="size-4" />
								</button>
								<button
									type="button"
									class="flex size-10 items-center justify-center rounded-lg border border-gray-300 text-gray-700 hover:border-red-300 hover:bg-red-50 hover:text-red-700 dark:border-gray-700 dark:text-gray-200 dark:hover:border-red-900 dark:hover:bg-red-950 dark:hover:text-red-200"
									on:click={() => (pendingDelete = memory)}
									aria-label="Удалить запись"
									title="Удалить"
								>
									<TrashIcon className="size-4" />
								</button>
							</div>
						</div>
					</article>
				{/each}
			</div>
		{/if}
	</div>
</div>

<MemoryModal
	bind:show={showMemoryModal}
	memory={selectedMemory as any}
	simple={true}
	on:save={loadMemories}
/>

<ConfirmDialog
	title="Удалить запись?"
	show={pendingDelete !== null}
	on:confirm={deleteMemory}
	on:cancel={() => (pendingDelete = null)}
>
	<div class="text-sm text-gray-600 dark:text-gray-300">
		Эта информация больше не будет использоваться в будущих разговорах.
		{#if pendingDelete}
			<div
				class="mt-3 max-h-32 overflow-y-auto rounded-lg bg-gray-50 p-3 text-gray-800 dark:bg-gray-900 dark:text-gray-200"
			>
				{pendingDelete.content}
			</div>
		{/if}
	</div>
</ConfirmDialog>
