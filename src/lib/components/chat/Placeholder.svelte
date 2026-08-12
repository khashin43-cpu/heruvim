<script lang="ts">
	import { createEventDispatcher, getContext, onMount } from 'svelte';
	import { fade } from 'svelte/transition';

	import { user, temporaryChatEnabled, selectedFolder } from '$lib/stores';
	import { refreshChatList } from '$lib/stores/chatList';

	import EyeSlash from '$lib/components/icons/EyeSlash.svelte';
	import DocumentIcon from '$lib/components/icons/Document.svelte';
	import Tooltip from '$lib/components/common/Tooltip.svelte';
	import MessageInput from './MessageInput.svelte';
	import FolderPlaceholder from './Placeholder/FolderPlaceholder.svelte';
	import FolderTitle from './Placeholder/FolderTitle.svelte';

	const dispatch = createEventDispatcher();
	const i18n = getContext('i18n');

	export let createMessagePair: Function;
	export let stopResponse: Function;
	export let autoScroll = false;
	export let atSelectedModel: Model | undefined;
	export let selectedModels: string[] = [];
	export let history;
	export let prompt = '';
	export let files = [];
	export let messageInput = null;
	export let selectedToolIds = [];
	export let selectedSkillIds = [];
	export let selectedFilterIds = [];
	export let pendingOAuthTools = [];
	export let showCommands = false;
	export let imageGenerationEnabled = false;
	export let codeInterpreterEnabled = false;
	export let webSearchEnabled = false;
	export let onUpload: Function = (e) => {};
	export let onSelect = (e) => {};
	export let onChange = (e) => {};
	export let onWebSearchToggle: Function = () => {};
	export let toolServers = [];
	export let dragged = false;

	let greeting = 'Добрый день';

	const executivePrompts = [
		{
			title: 'Изучить документ',
			description: 'Суть, цифры и выводы',
			prompt:
				'Изучите приложенный документ. Сначала дайте краткую суть, затем перечислите важные цифры, условия и выводы.'
		},
		{
			title: 'Найти в базе знаний',
			description: 'Поиск по всем документам',
			prompt:
				'Найдите в базе знаний документы по моему вопросу и ответьте со ссылками на найденные источники.'
		},
		{
			title: 'Сравнить документы',
			description: 'Различия и важные изменения',
			prompt:
				'Сравните приложенные документы. Покажите существенные различия, изменённые суммы, сроки и обязательства.'
		},
		{
			title: 'Проверить договор',
			description: 'Обязательства, сроки и риски',
			prompt:
				'Проверьте приложенный договор. Выделите обязательства сторон, суммы, сроки, штрафы и существенные риски.'
		}
	];

	$: folderReadOnly =
		$selectedFolder != null &&
		$selectedFolder.user_id !== $user?.id &&
		$selectedFolder.permission !== 'write';

	onMount(() => {
		const hour = new Date().getHours();
		greeting =
			hour < 6
				? 'Доброй ночи'
				: hour < 12
					? 'Доброе утро'
					: hour < 18
						? 'Добрый день'
						: 'Добрый вечер';
	});
</script>

<div class="heruvim-welcome m-auto w-full max-w-[72rem] px-4 py-16 @md:px-8 @2xl:px-14">
	{#if $temporaryChatEnabled}
		<Tooltip
			content={$i18n.t("This chat won't appear in history and your messages will not be saved.")}
			className="mb-4 flex w-full justify-center"
			placement="top"
		>
			<div
				class="flex w-fit items-center gap-2 rounded-full border border-stone-200 bg-white/70 px-3 py-1.5 text-xs text-stone-600 dark:border-white/10 dark:bg-white/5 dark:text-stone-300"
			>
				<EyeSlash strokeWidth="2" className="size-3.5" /> Временный разговор
			</div>
		</Tooltip>
	{/if}

	{#if $selectedFolder}
		<div class="mx-auto max-w-3xl">
			<FolderTitle
				folder={$selectedFolder}
				readOnly={folderReadOnly}
				onUpdate={async () => {
					await refreshChatList(localStorage.token);
				}}
				onDelete={async () => {
					await refreshChatList(localStorage.token);
					selectedFolder.set(null);
				}}
			/>

			{#if !folderReadOnly}
				<div class="mt-6">
					<MessageInput
						bind:this={messageInput}
						{history}
						bind:selectedModels
						bind:files
						bind:prompt
						bind:autoScroll
						bind:selectedToolIds
						bind:selectedSkillIds
						bind:selectedFilterIds
						bind:imageGenerationEnabled
						bind:codeInterpreterEnabled
						bind:webSearchEnabled
						bind:atSelectedModel
						bind:showCommands
						bind:dragged
						{pendingOAuthTools}
						{toolServers}
						{stopResponse}
						{createMessagePair}
						placeholder="Спросите ХЕРУВИМа или приложите документ"
						{onChange}
						{onUpload}
						{onWebSearchToggle}
						executiveMode={true}
						on:chatVariables
						on:submit={(e) => dispatch('submit', e.detail)}
					/>
				</div>
			{/if}

			<div class="mt-8" in:fade={{ duration: 180 }}>
				<FolderPlaceholder folder={$selectedFolder} />
			</div>
		</div>
	{:else}
		<div class="mx-auto max-w-4xl" in:fade={{ duration: 180 }}>
			<div class="mb-8 flex flex-col items-center text-center">
				<img
					src="/heruvim-mark.svg"
					alt=""
						class="mb-5 size-16 rounded-lg shadow-sm"
				/>
				<div
						class="mb-3 flex items-center gap-2 text-sm font-medium text-[#55736b] dark:text-[#9ab7ae]"
				>
					<span class="size-2 rounded-full bg-emerald-600 shadow-[0_0_0_4px_rgba(5,150,105,0.12)]"
					></span>
					Готов к работе
				</div>
				<h1
						class="text-3xl font-medium text-[#18352f] dark:text-[#edf4f1] @md:text-4xl"
				>
					{greeting}{#if $user?.name}, {$user.name}{/if}
				</h1>
				<p
					class="mt-3 max-w-2xl text-base leading-7 text-stone-600 dark:text-stone-300 @md:text-lg"
				>
						Приложите документ или задайте вопрос по базе знаний. Я начну с сути и покажу источники.
				</p>
			</div>

			<div
					class="heruvim-input mx-auto max-w-3xl rounded-lg border border-[#d7d2c5] bg-white p-2 shadow-sm dark:border-white/10 dark:bg-[#18211e]"
			>
				<MessageInput
					bind:this={messageInput}
					{history}
					bind:selectedModels
					bind:files
					bind:prompt
					bind:autoScroll
					bind:selectedToolIds
					bind:selectedSkillIds
					bind:selectedFilterIds
					bind:imageGenerationEnabled
					bind:codeInterpreterEnabled
					bind:webSearchEnabled
					bind:atSelectedModel
					bind:showCommands
					bind:dragged
					{pendingOAuthTools}
					{toolServers}
					{stopResponse}
					{createMessagePair}
					placeholder="Спросите, продиктуйте поручение или приложите документ"
					{onChange}
					{onUpload}
					{onWebSearchToggle}
					executiveMode={true}
					on:chatVariables
					on:submit={(e) => dispatch('submit', e.detail)}
				/>
			</div>

			<div class="mx-auto mt-7 grid max-w-3xl grid-cols-1 gap-3 @md:grid-cols-2">
					{#each executivePrompts as item}
					<button
						type="button"
							class="group flex min-h-[5rem] items-center gap-4 rounded-lg border border-[#ded9cd] bg-white px-5 py-4 text-left transition hover:border-[#8b7848] hover:bg-[#faf9f5] dark:border-white/10 dark:bg-white/[0.035] dark:hover:border-[#8f7b45] dark:hover:bg-white/[0.06]"
						on:click={() => onSelect({ type: 'prompt', data: item.prompt })}
					>
							<span class="flex size-9 shrink-0 items-center justify-center rounded-lg bg-[#e8e2d3] text-[#38584f] transition group-hover:bg-[#d7cba8] dark:bg-white/10 dark:text-[#d8e5e0]">
								<DocumentIcon className="size-5" strokeWidth="1.7" />
							</span>
						<span class="min-w-0">
							<span class="block text-[15px] font-medium text-[#213c35] dark:text-[#edf4f1]"
								>{item.title}</span
							>
							<span class="mt-1 block text-sm text-stone-500 dark:text-stone-400"
								>{item.description}</span
							>
						</span>
					</button>
				{/each}
			</div>
			</div>
	{/if}
</div>

<style>
	.heruvim-input :global(form) {
		border: 0;
		box-shadow: none;
	}
</style>
