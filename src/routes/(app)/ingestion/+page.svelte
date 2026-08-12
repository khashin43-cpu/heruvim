<script lang="ts">
	import { onDestroy, onMount } from 'svelte';
	import { toast } from 'svelte-sonner';

	import { uploadFile } from '$lib/apis/files';
	import {
		deleteKnowledgeDocument,
		getIngestionStatus,
		getKnowledgeDocumentBlob,
		getKnowledgeDocuments,
		retryIngestion
	} from '$lib/apis/heruvim';
	import ArrowPathIcon from '$lib/components/icons/ArrowPath.svelte';
	import CloudArrowUpIcon from '$lib/components/icons/CloudArrowUp.svelte';
	import DocumentIcon from '$lib/components/icons/Document.svelte';
	import DownloadIcon from '$lib/components/icons/Download.svelte';
	import EyeIcon from '$lib/components/icons/Eye.svelte';
	import SearchIcon from '$lib/components/icons/Search.svelte';
	import SidebarIcon from '$lib/components/icons/Sidebar.svelte';
	import TrashIcon from '$lib/components/icons/Trash.svelte';
	import { showSidebar } from '$lib/stores';

	type DatasetStatus = {
		dataset_id: string;
		document_id: string;
		state: string;
	};

	type Job = {
		file_id: string;
		filename: string;
		state: string;
		progress: number;
		message?: string;
		error?: string;
		datasets: DatasetStatus[];
	};

	type KnowledgeDocument = {
		id: string;
		dataset_id: string;
		name: string;
		type?: string;
		size?: number;
		chunk_num?: number;
		created_at?: number;
		updated_at?: number;
		can_delete?: boolean;
	};

	let loading = true;
	let uploading = false;
	let dragging = false;
	let retrying = '';
	let opening = '';
	let downloading = '';
	let deleting = '';
	let error = '';
	let search = '';
	let fileInput: HTMLInputElement;
	let pendingDelete: KnowledgeDocument | null = null;
	let timer: ReturnType<typeof setInterval> | undefined;

	let status: {
		enabled: boolean;
		configured: boolean;
		jobs: Job[];
	} = { enabled: false, configured: false, jobs: [] };
	let documents: KnowledgeDocument[] = [];

	const stateMeta: Record<string, { label: string; classes: string }> = {
		not_queued: { label: 'Ожидает', classes: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-200' },
		queued: { label: 'Ожидает', classes: 'bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-100' },
		uploading: { label: 'Загружается', classes: 'bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-100' },
		checking: { label: 'Проверяется', classes: 'bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-100' },
		indexing: { label: 'Обрабатывается', classes: 'bg-blue-100 text-blue-900 dark:bg-blue-950 dark:text-blue-100' },
		ready: { label: 'Готов', classes: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100' },
		failed: { label: 'Ошибка', classes: 'bg-red-100 text-red-900 dark:bg-red-950 dark:text-red-100' },
		cancelled: { label: 'Удалён', classes: 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-200' }
	};

	const meta = (state: string) => stateMeta[state] ?? stateMeta.not_queued;
	const formatSize = (value?: number) => {
		if (!value) return '';
		if (value < 1024 * 1024) return `${Math.max(1, Math.round(value / 1024))} КБ`;
		return `${(value / 1024 / 1024).toFixed(1)} МБ`;
	};
	const formatDate = (value?: number) => {
		if (!value) return '';
		const milliseconds = value > 10_000_000_000 ? value : value * 1000;
		return new Intl.DateTimeFormat('ru-RU', { day: '2-digit', month: 'long', year: 'numeric' }).format(milliseconds);
	};

	const load = async (quiet = false) => {
		if (!quiet) loading = true;
		try {
			const [ingestion, knowledge] = await Promise.all([
				getIngestionStatus(localStorage.token),
				getKnowledgeDocuments(localStorage.token)
			]);
			status = ingestion;
			documents = (knowledge.datasets ?? []).flatMap((dataset) =>
				(dataset.documents ?? []).map((document) => ({ ...document, dataset_id: dataset.dataset_id }))
			);
			error = '';
		} catch (exception) {
			error = exception instanceof Error ? exception.message : String(exception);
		} finally {
			loading = false;
		}
	};

	const addFiles = async (files: FileList | File[]) => {
		const selected = Array.from(files);
		if (!selected.length || uploading) return;
		uploading = true;
		try {
			for (const file of selected) {
				await uploadFile(localStorage.token, file, null, false, false);
			}
			toast.success(selected.length === 1 ? 'Документ добавлен' : `Добавлено документов: ${selected.length}`);
			await load(true);
		} catch (exception) {
			toast.error(exception instanceof Error ? exception.message : String(exception));
		} finally {
			uploading = false;
			if (fileInput) fileInput.value = '';
		}
	};

	const retry = async (job: Job) => {
		retrying = job.file_id;
		try {
			await retryIngestion(localStorage.token, job.file_id);
			toast.success('Повторная обработка началась');
			await load(true);
		} catch (exception) {
			toast.error(exception instanceof Error ? exception.message : String(exception));
		} finally {
			retrying = '';
		}
	};

	const openDocument = async (document: KnowledgeDocument) => {
		opening = document.id;
		try {
			const blob = await getKnowledgeDocumentBlob(localStorage.token, document.id);
			const url = URL.createObjectURL(blob);
			window.open(url, '_blank', 'noopener,noreferrer');
			setTimeout(() => URL.revokeObjectURL(url), 60_000);
		} catch (exception) {
			toast.error(exception instanceof Error ? exception.message : String(exception));
		} finally {
			opening = '';
		}
	};

	const downloadDocument = async (document: KnowledgeDocument) => {
		downloading = document.id;
		try {
			const blob = await getKnowledgeDocumentBlob(localStorage.token, document.id, true);
			const url = URL.createObjectURL(blob);
			const anchor = window.document.createElement('a');
			anchor.href = url;
			anchor.download = document.name || 'document';
			anchor.click();
			URL.revokeObjectURL(url);
		} catch (exception) {
			toast.error(exception instanceof Error ? exception.message : String(exception));
		} finally {
			downloading = '';
		}
	};

	const deleteDocument = async () => {
		if (!pendingDelete) return;
		deleting = pendingDelete.id;
		try {
			await deleteKnowledgeDocument(localStorage.token, pendingDelete.dataset_id, pendingDelete.id);
			documents = documents.filter((document) => document.id !== pendingDelete?.id);
			toast.success('Документ удалён из базы знаний');
			pendingDelete = null;
			await load(true);
		} catch (exception) {
			toast.error(exception instanceof Error ? exception.message : String(exception));
		} finally {
			deleting = '';
		}
	};

	onMount(() => {
		load();
		timer = setInterval(() => load(true), 5000);
	});

	onDestroy(() => {
		if (timer) clearInterval(timer);
	});

	$: activeJobs = status.jobs.filter((job) => ['queued', 'uploading', 'checking', 'indexing', 'failed'].includes(job.state));
	$: readyCount = documents.length;
	$: filteredDocuments = documents.filter((document) => document.name?.toLowerCase().includes(search.trim().toLowerCase()));
</script>

<svelte:head>
	<title>База знаний — ХЕРУВИМ</title>
</svelte:head>

<div
	class="h-full w-full min-w-0 overflow-y-auto bg-white transition-[max-width] dark:bg-gray-950 {$showSidebar
		? 'md:max-w-[calc(100%-var(--sidebar-width))]'
		: ''}"
>
	<div class="mx-auto w-full max-w-6xl px-4 py-6 md:px-8 md:py-9">
		<header class="mb-6 flex items-start justify-between gap-4">
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
					<h1 class="text-2xl font-semibold text-gray-950 dark:text-white md:text-3xl">База знаний</h1>
					<p class="mt-2 text-base text-gray-600 dark:text-gray-300">Документы, которыми пользуется ХЕРУВИМ</p>
				</div>
			</div>
			<button
				type="button"
				class="flex size-11 shrink-0 items-center justify-center rounded-lg border border-gray-200 text-gray-700 hover:bg-gray-50 dark:border-gray-700 dark:text-gray-200 dark:hover:bg-gray-900"
				on:click={() => load()}
				aria-label="Обновить список"
				title="Обновить список"
			>
				<ArrowPathIcon className="size-5" strokeWidth="1.8" />
			</button>
		</header>

		<input
			bind:this={fileInput}
			type="file"
			multiple
			accept=".pdf,.docx,.xlsx,.txt,.md,.csv,.rtf"
			class="hidden"
			on:change={(event) => addFiles(event.currentTarget.files ?? [])}
		/>
		<button
			type="button"
			class="mb-7 flex min-h-32 w-full items-center justify-center gap-4 rounded-lg border-2 border-dashed px-5 py-6 text-left transition {dragging
				? 'border-emerald-600 bg-emerald-50 dark:bg-emerald-950/30'
				: 'border-gray-300 bg-gray-50 hover:border-emerald-600 hover:bg-emerald-50/60 dark:border-gray-700 dark:bg-gray-900 dark:hover:border-emerald-500'}"
			on:click={() => fileInput?.click()}
			on:dragenter|preventDefault={() => (dragging = true)}
			on:dragover|preventDefault={() => (dragging = true)}
			on:dragleave|preventDefault={() => (dragging = false)}
			on:drop|preventDefault={(event) => {
				dragging = false;
				addFiles(event.dataTransfer?.files ?? []);
			}}
		>
			<div class="flex size-12 shrink-0 items-center justify-center rounded-lg bg-emerald-900 text-white dark:bg-emerald-700">
				<CloudArrowUpIcon className="size-6" strokeWidth="1.8" />
			</div>
			<div>
				<div class="text-lg font-semibold text-gray-950 dark:text-white">{uploading ? 'Документы добавляются…' : 'Добавить документы'}</div>
				<div class="mt-1 text-sm text-gray-500 dark:text-gray-400">PDF, Word, Excel и текстовые файлы</div>
			</div>
		</button>

		{#if !status.configured}
			<div class="mb-6 rounded-lg border border-amber-300 bg-amber-50 p-4 text-base text-amber-950 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
				База знаний временно недоступна. Обратитесь к администратору.
			</div>
		{:else if error}
			<div class="mb-6 rounded-lg border border-red-200 bg-red-50 p-4 text-base text-red-900 dark:border-red-900 dark:bg-red-950/40 dark:text-red-100">{error}</div>
		{/if}

		{#if activeJobs.length > 0}
			<section class="mb-8" aria-labelledby="processing-title">
				<h2 id="processing-title" class="mb-3 text-lg font-semibold text-gray-950 dark:text-white">Обработка</h2>
				<div class="divide-y divide-gray-200 rounded-lg border border-gray-200 dark:divide-gray-800 dark:border-gray-800">
					{#each activeJobs as job (job.file_id)}
						<div class="flex flex-col gap-3 p-4 md:flex-row md:items-center">
							<DocumentIcon className="size-6 shrink-0 text-gray-500" strokeWidth="1.6" />
							<div class="min-w-0 flex-1">
								<div class="truncate text-base font-medium text-gray-950 dark:text-white">{job.filename}</div>
								<div class="mt-2 h-2 overflow-hidden rounded-full bg-gray-100 dark:bg-gray-800">
									<div class="h-full rounded-full bg-emerald-700 transition-all" style="width: {Math.max(3, Math.min(100, job.progress || 0))}%"></div>
								</div>
								{#if job.error}<div class="mt-2 text-sm text-red-700 dark:text-red-300">{job.error}</div>{/if}
							</div>
							<span class="w-fit rounded-full px-3 py-1 text-sm font-medium {meta(job.state).classes}">{meta(job.state).label}</span>
							{#if job.state === 'failed'}
								<button
									type="button"
									class="min-h-10 rounded-lg border border-gray-300 px-4 text-sm font-medium hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-900"
									disabled={retrying === job.file_id}
									on:click={() => retry(job)}
								>Повторить</button>
							{/if}
						</div>
					{/each}
				</div>
			</section>
		{/if}

		<section aria-labelledby="documents-title">
			<div class="mb-4 flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
				<h2 id="documents-title" class="text-lg font-semibold text-gray-950 dark:text-white">Документы <span class="font-normal text-gray-500">{readyCount}</span></h2>
				{#if documents.length > 5}
					<label class="relative block w-full md:w-80">
						<SearchIcon className="pointer-events-none absolute left-3.5 top-3.5 size-5 text-gray-400" strokeWidth="1.7" />
						<input bind:value={search} class="h-12 w-full rounded-lg border border-gray-300 bg-white pl-11 pr-4 text-base outline-none focus:border-emerald-700 dark:border-gray-700 dark:bg-gray-900" placeholder="Найти документ" />
					</label>
				{/if}
			</div>

			{#if loading}
				<div class="py-16 text-center text-base text-gray-500">Загрузка…</div>
			{:else if filteredDocuments.length === 0}
				<div class="rounded-lg border border-gray-200 px-6 py-14 text-center dark:border-gray-800">
					<DocumentIcon className="mx-auto size-9 text-gray-400" strokeWidth="1.5" />
					<div class="mt-3 text-lg font-medium">{search ? 'Документ не найден' : 'Документов пока нет'}</div>
				</div>
			{:else}
				<div class="divide-y divide-gray-200 rounded-lg border border-gray-200 dark:divide-gray-800 dark:border-gray-800">
					{#each filteredDocuments as document (document.id)}
						<div class="flex flex-col gap-4 p-4 md:flex-row md:items-center">
							<div class="flex min-w-0 flex-1 items-center gap-3">
								<div class="flex size-11 shrink-0 items-center justify-center rounded-lg bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-200">
									<DocumentIcon className="size-6" strokeWidth="1.6" />
								</div>
								<div class="min-w-0">
									<div class="truncate text-base font-medium text-gray-950 dark:text-white">{document.name}</div>
									<div class="mt-1 flex flex-wrap gap-x-3 text-sm text-gray-500">
										{#if document.size}<span>{formatSize(document.size)}</span>{/if}
										{#if document.updated_at || document.created_at}<span>{formatDate(document.updated_at || document.created_at)}</span>{/if}
									</div>
								</div>
							</div>
							<div class="flex flex-wrap items-center gap-2 md:justify-end">
								<button type="button" class="flex min-h-11 items-center gap-2 rounded-lg border border-gray-300 px-4 text-sm font-medium hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-900" disabled={opening === document.id} on:click={() => openDocument(document)}>
									<EyeIcon className="size-5" strokeWidth="1.7" /> Открыть
								</button>
								<button type="button" class="flex size-11 items-center justify-center rounded-lg border border-gray-300 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-700 dark:hover:bg-gray-900" disabled={downloading === document.id} on:click={() => downloadDocument(document)} aria-label="Скачать документ" title="Скачать документ">
									<DownloadIcon className="size-5" strokeWidth="1.7" />
								</button>
								{#if document.can_delete}
									<button type="button" class="flex size-11 items-center justify-center rounded-lg border border-red-200 text-red-700 hover:bg-red-50 dark:border-red-900 dark:text-red-300 dark:hover:bg-red-950/40" on:click={() => (pendingDelete = document)} aria-label="Удалить документ" title="Удалить документ">
										<TrashIcon className="size-5" strokeWidth="1.7" />
									</button>
								{/if}
							</div>
						</div>
					{/each}
				</div>
			{/if}
		</section>
	</div>
</div>

{#if pendingDelete}
	<div class="fixed inset-0 z-[100] flex items-center justify-center p-4">
		<button class="absolute inset-0 bg-black/50" type="button" aria-label="Закрыть окно удаления" on:click={() => (pendingDelete = null)}></button>
		<div class="relative z-10 w-full max-w-md rounded-lg bg-white p-6 shadow-xl dark:bg-gray-900" role="dialog" aria-modal="true" aria-labelledby="delete-title" tabindex="-1">
			<h2 id="delete-title" class="text-xl font-semibold">Удалить документ?</h2>
			<p class="mt-3 break-words text-base text-gray-600 dark:text-gray-300">{pendingDelete.name}</p>
			<div class="mt-6 flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
				<button type="button" class="min-h-11 rounded-lg border border-gray-300 px-5 text-base font-medium hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-gray-800" on:click={() => (pendingDelete = null)}>Отмена</button>
				<button type="button" class="min-h-11 rounded-lg bg-red-700 px-5 text-base font-medium text-white hover:bg-red-800 disabled:opacity-50" disabled={deleting === pendingDelete.id} on:click={deleteDocument}>{deleting ? 'Удаление…' : 'Удалить'}</button>
			</div>
		</div>
	</div>
{/if}
