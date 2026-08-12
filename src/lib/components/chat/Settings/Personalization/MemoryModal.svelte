<script lang="ts">
	import { createEventDispatcher, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as I18n } from 'i18next';
	import { toast } from 'svelte-sonner';

	import { addNewMemory, updateMemoryById } from '$lib/apis/memories';

	import Modal from '$lib/components/common/Modal.svelte';
	import Spinner from '$lib/components/common/Spinner.svelte';
	import XMark from '$lib/components/icons/XMark.svelte';

	const dispatch = createEventDispatcher();

	type Memory = {
		id: string;
		content: string;
		type?: string;
		path?: string;
	};

	export let show: boolean;
	export let memory: Memory | null = null;
	export let simple = false;

	const i18n = getContext<Writable<I18n>>('i18n');

	let loading = false;
	let content = '';
	let type = 'user';
	let path = '';

	$: edit = !!memory?.id;
	$: if (show) {
		content = memory?.content ?? '';
		type = simple ? 'user' : (memory?.type ?? 'user');
		path = memory?.path ?? '';
	}

	const submitHandler = async () => {
		loading = true;

		const request =
			edit && memory
				? updateMemoryById(localStorage.token, memory.id, content, type, path)
				: addNewMemory(localStorage.token, content, type, path);
		const res = await request.catch((error) => {
			toast.error(`${error}`);
			return null;
		});

		if (res) {
			toast.success(
				edit ? $i18n.t('Memory updated successfully') : $i18n.t('Memory added successfully')
			);
			show = false;
			dispatch('save');
		}

		loading = false;
	};
</script>

<Modal bind:show size="sm">
	<div>
		<div class=" flex justify-between dark:text-gray-300 px-5 pt-4 pb-2">
			<div class=" text-lg font-medium self-center">
				{#if simple && edit}
					Исправить запись
				{:else if simple}
					Добавить в память
				{:else if edit}
					{$i18n.t('Edit Memory')}
				{:else}
					{$i18n.t('Add Memory')}
				{/if}
			</div>
			<button
				type="button"
				class="self-center"
				on:click={() => (show = false)}
				aria-label={simple ? 'Закрыть' : $i18n.t('Close')}
			>
				<XMark className={'size-5'} />
			</button>
		</div>

		<div class="flex flex-col md:flex-row w-full px-4 pb-4 md:space-x-4 dark:text-gray-200">
			<div class=" flex flex-col w-full sm:flex-row sm:justify-center sm:space-x-6">
				<form class="flex flex-col w-full" on:submit|preventDefault={submitHandler}>
					<div class="px-1">
						{#if !simple}
							<div class="flex w-full justify-between items-center mb-1.5">
								<div class="text-xs text-gray-500">{$i18n.t('Type')}</div>

								<button
									type="button"
									class="text-xs text-gray-700 dark:text-gray-300"
									on:click={() => {
										type = type === 'user' ? 'context' : 'user';
									}}
								>
									{#if type === 'user'}
										{$i18n.t('User')}
									{:else}
										{$i18n.t('Context')}
									{/if}
								</button>
							</div>
						{:else}
							<label
								for="simple-memory-content"
								class="mb-2 block text-sm text-gray-600 dark:text-gray-300"
							>
								Что ХЕРУВИМ должен учитывать в будущих разговорах
							</label>
						{/if}

						<textarea
							id={simple ? 'simple-memory-content' : undefined}
							bind:value={content}
							class="w-full rounded-lg border border-gray-300 bg-transparent p-3 text-base outline-hidden placeholder:text-gray-400 focus:border-emerald-700 focus:ring-2 focus:ring-emerald-700/20 dark:border-gray-700 dark:placeholder:text-gray-600"
							rows="6"
							style="resize: vertical;"
							placeholder={simple
								? 'Например: предпочитаю краткие ответы без сложных терминов'
								: type === 'user'
									? $i18n.t('Add a preference, fact, or instruction about you')
									: $i18n.t('Add durable context for future chats')}
						></textarea>

						{#if !simple}
							<div class="flex flex-col w-full mt-1.5">
								<label for="memory-path" class="mb-0.5 text-xs text-gray-500">
									{$i18n.t('Path')}
									<span class="opacity-50">({$i18n.t('optional')})</span>
								</label>

								<input
									id="memory-path"
									bind:value={path}
									class="w-full text-sm bg-transparent outline-hidden placeholder:text-gray-300 dark:placeholder:text-gray-700"
									placeholder={$i18n.t('Path')}
									autocomplete="off"
								/>
							</div>
						{/if}
					</div>

					<div class="flex justify-end pt-1 text-sm font-medium">
						<button
							class="px-3.5 py-1.5 text-sm font-medium bg-black hover:bg-gray-900 text-white dark:bg-white dark:text-black dark:hover:bg-gray-100 transition rounded-full flex items-center gap-2 whitespace-nowrap {loading
								? ' cursor-not-allowed'
								: ''}"
							type="submit"
							disabled={loading}
						>
							{#if simple && edit}
								Сохранить
							{:else if simple}
								Добавить
							{:else if edit}
								{$i18n.t('Update')}
							{:else}
								{$i18n.t('Add')}
							{/if}

							{#if loading}
								<span class="shrink-0">
									<Spinner />
								</span>
							{/if}
						</button>
					</div>
				</form>
			</div>
		</div>
	</div>
</Modal>
