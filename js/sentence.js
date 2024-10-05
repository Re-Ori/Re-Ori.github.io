function loadSentence() {
    fetch('json/sentence.json')
        .then(response => response.json())
        .then(data => {
            const randomIndex = Math.floor(Math.random() * data.length);
            const sentence = data[randomIndex].sentence;
            const sentence_from = data[randomIndex].from;
            const sentence_id = data[randomIndex].id;
            document.getElementById('sentence').style.whiteSpace = 'pre-line';
            document.getElementById('sentence').textContent = `「${sentence}」\n——${sentence_from}\n[ID ${sentence_id}]`;

            document.getElementById('sentence').addEventListener('click', () => {
                // 点击一言复制到剪切板
                navigator.clipboard.writeText(`「${sentence}」——${sentence_from}`)
                document.getElementById('sentence').textContent = `「${sentence}」\n——${sentence_from}\n[已复制]`;
                document.getElementById('sentence').style.color = "#16c000";
                setTimeout(() => document.getElementById('sentence').textContent = `「${sentence}」\n——${sentence_from}\n[ID ${sentence_id}]`, 300)
                setTimeout(() => document.getElementById('sentence').style.color = "", 300)
            });
        })
        .catch(error => console.error('Error loading the JSON file:', error));
}


document.getElementById('refresh-btn').addEventListener('click', () => {
    loadSentence();
});

loadSentence();